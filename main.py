import asyncio
import html
import os
import signal
from datetime import datetime, timezone
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BufferedInputFile,
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
from config import MODELS, MODEL_STRATEGIES, PLANS, is_admin
from db.database import get_session, init_db  # noqa: F401
from deployer import (
    deploy_bot,
    redeploy_bot,
    get_bot_logs,
    get_bot_status,
    prepare_bot_files,
    remove_bot,
    stop_bot,
)
from db.repository import (
    anonymize_user,
    count_client_bots,
    delete_bot,
    get_active_subscription,
    get_bot_by_id,
    get_bot_stats,
    get_chat_history,
    get_client_bots,
    get_daily_usage,
    get_limit_alerts_enabled,
    get_or_create_client,
    get_usage_by_bot,
    get_usage_by_model,
    get_usage_stats,
    get_usage_trend,
    log_tokens,
    save_bot_config,
    save_chat_message,
    save_consent,
    set_bot_status,
    set_limit_alerts,
    update_bot_config,
    update_bot_system_prompt,
)
from pipeline import (
    _token_accumulator,
    regenerate_system_prompt,
    run_bot_query,
    run_pipeline,
)
from services.alerts import start_alerts_scheduler
from services.image_generation import image_generator
from services.rag import (
    add_knowledge,
    clear_knowledge,
    count_knowledge,
    list_knowledge_sources,
    search_knowledge,
)
from templates.bot_questionnaires import QUESTIONNAIRES, is_sensitive_question
from webhook_server import start_webhook_server

CONSENT_TEXT = """Для создания бота мы обрабатываем ваш Telegram ID и username.
Данные хранятся на серверах в России, третьим лицам не передаются.
Вы можете удалить свои данные командой /delete_my_data

Нажмите Согласен чтобы продолжить."""


class IntakeStates(StatesGroup):
    consent = State()
    ask_type = State()
    answering = State()
    clarifying = State()
    ask_bot_token = State()
    processing = State()


class InlineChatStates(StatesGroup):
    chatting = State()


class TeachStates(StatesGroup):
    receiving = State()


class ImageStates(StatesGroup):
    waiting_prompt = State()


class EditStates(StatesGroup):
    waiting_prompt = State()
    waiting_forbidden = State()
    waiting_scripts = State()
    waiting_greeting = State()


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


# Persistent main menu shown after consent. 4×2 layout. Admins see
# "💎 Безлимит (админ)" instead of "💎 Тариф" — they have an unlimited
# tier and don't need /subscribe.
_MAIN_MENU_BUTTONS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("💬 Чат с ботом", "/chat"), ("🤖 Мои боты", "/mybots")),
    (("📊 Статистика", "/usage"), ("📚 Обучение", "/teach")),
    (("⚙️ Настройки", "/settings"), ("💎 Тариф", "/subscribe")),
    (("➕ Создать бота", "/start"), ("❓ Помощь", "/help")),
)
_MAIN_MENU_ADMIN_LABEL = "💎 Безлимит (админ)"
_MAIN_MENU_LABELS_ALL: set[str] = {
    label for row in _MAIN_MENU_BUTTONS for label, _cmd in row
} | {_MAIN_MENU_ADMIN_LABEL}


def _main_menu_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    """Persistent main menu (4×2). For admins the 💎 Тариф slot is
    swapped to a non-actionable 'Безлимит (админ)' label."""
    rows = []
    for row in _MAIN_MENU_BUTTONS:
        kb_row = []
        for label, _cmd in row:
            if is_admin_user and label == "💎 Тариф":
                kb_row.append(KeyboardButton(text=_MAIN_MENU_ADMIN_LABEL))
            else:
                kb_row.append(KeyboardButton(text=label))
        rows.append(kb_row)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


@router.message(F.text.in_(_MAIN_MENU_LABELS_ALL))
async def on_main_menu_button(message: Message, state: FSMContext) -> None:
    """FSM intercept (variant β): tapping any main-menu button at any
    point clears the current FSM state and dispatches to the matching
    command handler. Registered FIRST in the router so it fires before
    any waiting-for-input FSM handler steals the message.

    Without this, taps during intake/edit FSMs would be swallowed by
    `on_answer` / `on_edit_*` and the user would feel the menu is broken.
    """
    user = message.from_user
    label = (message.text or "").strip()
    is_admin_user = bool(user and is_admin(user.id))

    if label == _MAIN_MENU_ADMIN_LABEL:
        await state.clear()
        await message.answer(
            "💎 У вас безлимитный тариф (admin). /usage — статистика по ботам.",
            reply_markup=_main_menu_keyboard(is_admin_user=True),
        )
        return

    await state.clear()

    if label == "💬 Чат с ботом":
        await cmd_chat(message, state)
    elif label == "🤖 Мои боты":
        await cmd_mybots(message)
    elif label == "📊 Статистика":
        await cmd_usage(message)
    elif label == "📚 Обучение":
        await cmd_teach(message, state)
    elif label == "⚙️ Настройки":
        await cmd_settings(message)
    elif label == "💎 Тариф":
        await cmd_subscribe(message)
    elif label == "➕ Создать бота":
        await cmd_start(message, state)
    elif label == "❓ Помощь":
        await cmd_help(message)


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
    is_admin_user = is_admin(user.id)

    # Bots-limit guard: block before the user invests time in the
    # questionnaire. Slot may free up later via /mybots delete — they
    # can re-enter intake then.
    client = await get_or_create_client(user.id, user.username)
    allowed, plan_name, count, limit = await _check_bots_limit(client.id, user.id)
    if not allowed:
        await state.clear()
        await message.answer(
            f"⚠️ У вас уже {count} ботов из {limit} на тарифе {plan_name}.\n"
            "Создание нового невозможно — удалите существующего "
            "через 🤖 Мои боты или апгрейд тарифа через 💎 Тариф.",
            reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
        )
        return

    await state.set_state(IntakeStates.ask_type)
    # Install the persistent main menu now that the user has consented.
    # Inline keyboard for bot-type selection coexists with it (different layer).
    await message.answer(
        "Согласие сохранено. Меню всегда доступно внизу экрана.",
        reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
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


def _redact_sensitive(raw_answers: dict) -> tuple[dict, int]:
    """Strip sensitive answer values before sending to LLM. Returns
    (safe_answers, sensitive_count). Question text is preserved so the
    analyst can mark the secret as "provided" via a placeholder flag."""
    llm_answers: dict = {}
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
    return llm_answers, sensitive_count


ASK_TOKEN_PROMPT = (
    "Отлично, все вопросы собраны!\n\n"
    "Теперь создайте бота у @BotFather командой /newbot, "
    "получите токен и отправьте его сюда."
)


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

    llm_answers, _ = _redact_sensitive(answers)
    # Import lazily — top-level import would make main.py load agents.analyst
    # before pipeline.py, breaking the existing analyst<->pipeline import dance.
    from agents.analyst import check_completeness

    try:
        clarifying_qs = await asyncio.to_thread(
            check_completeness, llm_answers
        )
    except Exception:
        logger.exception("intake: check_completeness failed")
        clarifying_qs = []

    if clarifying_qs:
        await state.update_data(
            clarification_questions=clarifying_qs,
            clarification_current=0,
            clarification_answers={},
        )
        await state.set_state(IntakeStates.clarifying)
        await message.answer(
            "Почти готово! Уточню несколько деталей для "
            "лучшего результата:"
        )
        await message.answer(clarifying_qs[0])
        return

    await state.set_state(IntakeStates.ask_bot_token)
    await message.answer(ASK_TOKEN_PROMPT)


@router.message(IntakeStates.clarifying)
async def on_clarifying(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    questions = data.get("clarification_questions") or []
    idx = data.get("clarification_current", 0)
    answers = dict(data.get("clarification_answers") or {})

    if idx >= len(questions):
        await state.set_state(IntakeStates.ask_bot_token)
        await message.answer(ASK_TOKEN_PROMPT)
        return

    answer_text = (message.text or "").strip()
    answers[str(idx)] = {
        "question": questions[idx],
        "answer": answer_text,
    }

    next_idx = idx + 1
    await state.update_data(
        clarification_answers=answers,
        clarification_current=next_idx,
    )

    if next_idx < len(questions):
        await message.answer(questions[next_idx])
        return

    await state.set_state(IntakeStates.ask_bot_token)
    await message.answer(ASK_TOKEN_PROMPT)


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
    clarification_answers: dict = data.get("clarification_answers") or {}

    llm_answers, sensitive_count = _redact_sensitive(raw_answers)

    pipeline_input = {
        "bot_type": bot_type,
        "questionnaire_type": bot_type,
        "answers": llm_answers,
        "clarification_answers": clarification_answers,
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

        # Second bots-limit gate: between consent and token submission a
        # parallel session could have created bots, or the client could
        # have used multiple devices to start two intakes at once. Pipeline
        # has already run (tokens spent) — log warning if we have to bail.
        allowed, plan_name, count, limit = await _check_bots_limit(client.id, user.id)
        if not allowed:
            logger.warning(
                "intake: bots_limit hit AFTER pipeline for client_id={} "
                "(tokens already spent, count={}/{} on {})",
                client.id,
                count,
                limit,
                plan_name,
            )
            await message.answer(
                f"🚫 Достигнут лимит ботов на вашем тарифе.\n\n"
                f"Текущий тариф: {plan_name} ({limit})\n"
                f"Активных ботов: {count}\n\n"
                "Варианты:\n"
                "• Удалите ненужного бота через 🤖 Мои боты\n"
                "• Перейдите на тариф выше → 💎 Тариф",
                reply_markup=_main_menu_keyboard(is_admin_user=is_admin(user.id)),
            )
            await state.clear()
            return

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
                "clarification_answers": clarification_answers,
            },
            bot_token=bot_token,
        )
        # Files live under bots/{bot_id}/ so multiple bots per client don't
        # collide; deployer uses the same path.
        prepare_bot_files(spec.bot_code, saved_bot.id)
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
        "intake: pipeline ok for client_id={} bot_id={} (code_len={} bytes)",
        client.id,
        saved_bot.id,
        len(spec.bot_code),
    )

    deploy_ok = False
    try:
        await deploy_bot(saved_bot.id)
        deploy_ok = True
    except Exception:
        logger.exception(
            "intake: deploy_bot failed for bot_id={}", saved_bot.id
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
    if deploy_ok:
        await message.answer(
            f"✅ Бот готов и запущен в контейнере!\n\nТип: {resolved_type}\n"
            f"Контейнер: bot_client_{saved_bot.id}\n\n"
            "Оформите подписку /subscribe чтобы открыть доступ клиентам.",
            reply_markup=post_create_kb,
        )
    else:
        await message.answer(
            f"⚠️ Бот создан, но контейнер не поднялся.\n\nТип: {resolved_type}\n"
            f"Файл: bots/{saved_bot.id}/main.py сохранён.\n\n"
            "Напишите в поддержку: вероятно конфликт токена или ошибка в коде бота. "
            "Можно посмотреть логи через /mybots → карточка бота.",
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


def _progress_bar_used(
    used: int, limit: int, width: int = 10
) -> tuple[str, int]:
    """Bar filled in proportion to used/limit. pct is % used."""
    if limit <= 0:
        return "░" * width, 0
    ratio = max(0.0, min(1.0, used / limit))
    filled = round(width * ratio)
    pct = round(100 * ratio)
    return "█" * filled + "░" * (width - filled), pct


def _upgrade_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Апгрейд тарифа ↗",
                    callback_data="usage:upgrade",
                )
            ]
        ]
    )


async def _active_bot_name(client_id: int) -> str:
    bots = await get_client_bots(client_id)
    active = [b for b in bots if b.is_active]
    if not active:
        return "не создан"
    return active[0].bot_name


def _format_ru_date_short(dt) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month - 1]}"


def _days_until(dt) -> int:
    if dt is None:
        return 0
    delta = dt - datetime.now(timezone.utc)
    return max(0, delta.days)


def _usage_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📈 История", callback_data="usage:history"
                ),
                InlineKeyboardButton(
                    text="⚙️ Тариф", callback_data="usage:upgrade"
                ),
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data="usage:back"
                ),
            ]
        ]
    )


def _format_trend_block(trend: dict) -> str:
    lines = [
        "Динамика:",
        f"Сегодня:       {_format_num(trend['today'])} ток",
        f"Вчера:         {_format_num(trend['yesterday'])} ток",
        f"Эта неделя:    {_format_num(trend['this_week'])} ток",
        f"Прошлая нед:   {_format_num(trend['last_week'])} ток",
    ]
    growth = trend.get("growth_pct")
    if growth is None:
        lines.append("→ нет данных за прошлую неделю")
    elif growth > 0:
        lines.append(f"→ рост на {growth}%")
    elif growth < 0:
        lines.append(f"→ падение на {abs(growth)}%")
    else:
        lines.append("→ без изменений")
    return "\n".join(lines)


async def _render_usage_main(
    client_id: int, telegram_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    if is_admin(telegram_id):
        return (
            "📊 Использование токенов\n\n"
            "Тариф: Безлимит (админ)\n\n"
            "██████████ ∞ безлимит"
        ), None

    stats = await get_usage_stats(client_id)
    tier = stats["tier"]
    limit = stats["tokens_limit"]
    used = stats["tokens_used"]
    cost = stats["cost_usd_total"]
    reset_at = stats["reset_at"]

    if tier is None:
        return (
            "📊 Использование токенов\n\n"
            "Тариф: нет активной подписки\n\n"
            f"Использовано: {_format_num(used)}\n"
            f"💰 Потрачено: ${cost:.2f}\n\n"
            "Оформите подписку → /subscribe"
        ), _upgrade_keyboard()

    tier_label = PLANS[tier]["name"] if tier in PLANS else tier
    reset_short = _format_ru_date_short(reset_at) if reset_at else "—"
    days_to_reset = _days_until(reset_at)
    reset_line = (
        f"Сброс: через {days_to_reset} "
        f"{_ru_plural(days_to_reset, ('день', 'дня', 'дней'))} "
        f"({reset_short})"
    )

    header = (
        "📊 Использование за текущий период\n\n"
        f"Тариф: {tier_label}\n"
        f"{reset_line}\n\n"
    )

    # Business = unlimited tokens — skip bar and breakdown percentages.
    if limit is None:
        total_line = (
            f"Всего: {_format_num(used)} токенов\n"
            "██████████ ∞ безлимит\n"
        )
    else:
        bar, pct_used = _progress_bar_used(used, limit)
        total_line = (
            f"Всего: {_format_num(used)} / {_format_num(limit)} токенов\n"
            f"{bar} {pct_used}%\n"
        )

    # Breakdown since period_start (= last reset, 30 days before reset_at).
    period_start = (
        reset_at - timedelta(days=30)
        if reset_at is not None
        else datetime.now(timezone.utc) - timedelta(days=30)
    )
    breakdown = await get_usage_by_bot(client_id, period_start)
    bd_lines: list[str] = []
    if breakdown:
        bd_lines.append("\nПо ботам:")
        total = sum(b["tokens"] for b in breakdown) or 1
        for b in breakdown:
            pct = round(100 * b["tokens"] / total)
            bd_lines.append(
                f"🤖 {b['bot_name']} ({_bot_type_ru(b['bot_type'])}) — "
                f"{_format_num(b['tokens'])} ток ({pct}%)"
            )

    model_breakdown = await get_usage_by_model(client_id, period_start)
    md_lines: list[str] = []
    if model_breakdown:
        md_lines.append("\nПо моделям:")
        for m in model_breakdown:
            label = _TIER_LABEL_BY_SLUG.get(m["model"], m["model"])
            md_lines.append(
                f"{label}: {_format_num(m['tokens'])} ток "
                f"(${m['cost_usd']:.2f})"
            )

    trend = await get_usage_trend(client_id)
    text = (
        header
        + total_line
        + "\n".join(bd_lines)
        + ("\n" if bd_lines else "")
        + "\n".join(md_lines)
        + ("\n" if md_lines else "")
        + f"\n💰 Стоимость: ${cost:.2f}\n\n"
        + _format_trend_block(trend)
    )
    return text, _usage_main_keyboard()


_TIER_EMOJI = {"cheap": "💚", "balanced": "💛", "smart": "❤️"}
_TIER_RU = {
    "cheap": "Дешёвая",
    "balanced": "Сбалансированная",
    "smart": "Умная",
}


def _tier_by_slug(slug: str) -> str | None:
    for tier, s in MODELS.items():
        if s == slug:
            return tier
    return None


_TIER_LABEL_BY_SLUG: dict[str, str] = {
    slug: f"{_TIER_EMOJI[tier]} {_TIER_RU[tier]}"
    for tier, slug in MODELS.items()
}


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    text, kb = await _render_usage_main(client.id, user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "usage:upgrade")
async def cb_usage_upgrade(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer(
            "Выберите тариф подписки:", reply_markup=_subscribe_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data == "usage:back")
async def cb_usage_back(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except Exception:
            # Message too old to delete, or already gone.
            pass
    await callback.answer()


DAILY_CHART_DAYS = 14
DAILY_CHART_WIDTH = 10


def _render_daily_chart(daily: list[dict], max_tokens: int) -> str:
    lines = []
    scale = max_tokens or 1
    for row in daily:
        filled = round(DAILY_CHART_WIDTH * row["tokens"] / scale)
        filled = max(0, min(DAILY_CHART_WIDTH, filled))
        bar = "▓" * filled + "░" * (DAILY_CHART_WIDTH - filled)
        lines.append(
            f"{row['date'].strftime('%d.%m')} {bar}  "
            f"{_format_num(row['tokens'])}"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "usage:history")
async def cb_usage_history(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    daily = await get_daily_usage(client.id, days=DAILY_CHART_DAYS)
    total = sum(d["tokens"] for d in daily)
    max_tokens = max((d["tokens"] for d in daily), default=0)
    avg = total // DAILY_CHART_DAYS if DAILY_CHART_DAYS else 0

    chart = _render_daily_chart(daily, max_tokens)
    text_lines = [
        f"Потребление за {DAILY_CHART_DAYS} дней:\n",
        f"<pre>{html.escape(chart)}</pre>",
        "",
        f"Среднее: {_format_num(avg)} токенов/день",
    ]

    stats = await get_usage_stats(client.id)
    limit = stats["tokens_limit"]
    used = stats["tokens_used"]
    if limit is not None and avg > 0:
        tokens_left = max(0, limit - used)
        days_left = tokens_left // avg if avg else 0
        text_lines.append(
            f"При текущем темпе — хватит на {days_left} "
            f"{_ru_plural(days_left, ('день', 'дня', 'дней'))}"
        )
    elif limit is None and stats["tier"] is not None:
        text_lines.append("Лимит: безлимит")

    if callback.message is not None:
        await callback.message.answer(
            "\n".join(text_lines), parse_mode="HTML"
        )
    await callback.answer()


def _limit_alerts_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔔 Включены" if enabled else "🔕 Выключены",
                    callback_data="limit_alerts:noop",
                ),
                InlineKeyboardButton(
                    text="🔕 Отключить" if enabled else "🔔 Включить",
                    callback_data=(
                        "limit_alerts:off" if enabled else "limit_alerts:on"
                    ),
                ),
            ]
        ]
    )


_STRATEGY_META: dict[str, tuple[str, str]] = {
    "auto": ("🚀 Автоматически (экономия)", "Автоматически"),
    "smart": ("💎 Всегда умная", "Всегда умная"),
    "cheap": ("💰 Всегда дешёвая", "Всегда дешёвая"),
}


def _settings_keyboard(bot_id: int, current: str) -> InlineKeyboardMarkup:
    rows = []
    for strategy in MODEL_STRATEGIES:
        label, _ = _STRATEGY_META[strategy]
        prefix = "✅ " if strategy == current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{label}",
                    callback_data=f"settings:strategy:{bot_id}:{strategy}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _first_active_bot(client_id: int):
    bots = await get_client_bots(client_id)
    for b in bots:
        if b.is_active:
            return b
    return None


def _settings_text(bot_cfg, current: str) -> str:
    _, current_label = _STRATEGY_META.get(current, _STRATEGY_META["auto"])
    return (
        f"Настройки бота {bot_cfg.bot_name}\n\n"
        "Стратегия выбора модели:\n"
        "• 🚀 Автоматически — роутер выбирает дешёвую/среднюю/умную под каждый запрос\n"
        "• 💎 Всегда умная — качество важнее стоимости\n"
        "• 💰 Всегда дешёвая — максимальная экономия токенов\n\n"
        f"Текущая: {current_label}"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await _first_active_bot(client.id)
    if bot_cfg is None:
        await message.answer("У вас нет активного бота. /start чтобы создать")
        return
    current = (bot_cfg.config_json or {}).get("model_strategy", "auto")
    if current not in MODEL_STRATEGIES:
        current = "auto"
    await message.answer(
        _settings_text(bot_cfg, current),
        reply_markup=_settings_keyboard(bot_cfg.id, current),
    )


@router.callback_query(F.data.startswith("settings:strategy:"))
async def cb_settings_strategy(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    # settings:strategy:{bot_id}:{strategy}
    if len(parts) != 4:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    try:
        bot_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    strategy = parts[3]
    if strategy not in MODEL_STRATEGIES:
        await callback.answer("Неизвестная стратегия", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(
        bot_id, client.id, "model_strategy", strategy
    )
    if not ok:
        await callback.answer("Бот не найден", show_alert=True)
        return
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    if callback.message is not None and bot_cfg is not None:
        await callback.message.answer(
            _settings_text(bot_cfg, strategy),
            reply_markup=_settings_keyboard(bot_id, strategy),
        )
    await callback.answer("Сохранено")


@router.message(Command("limit_alerts"))
async def cmd_limit_alerts(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    # Touch the client row so the default (enabled=True) is persisted.
    await get_or_create_client(user.id, user.username)
    enabled = await get_limit_alerts_enabled(user.id)
    await message.answer(
        "Уведомления о лимите токенов\n\n"
        f"Статус: {'включены' if enabled else 'отключены'}\n"
        "Раз в день в 10:00 мы пишем, если у вас осталось меньше 30% месячного лимита.",
        reply_markup=_limit_alerts_keyboard(enabled),
    )


@router.callback_query(F.data.startswith("limit_alerts:"))
async def cb_limit_alerts(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    action = (callback.data or "").split(":", 1)[1]
    if action == "noop":
        await callback.answer()
        return
    if action not in ("on", "off"):
        await callback.answer()
        return
    await get_or_create_client(user.id, user.username)
    ok = await set_limit_alerts(user.id, action == "on")
    if not ok:
        await callback.answer("Не удалось обновить", show_alert=True)
        return
    enabled = action == "on"
    if callback.message is not None:
        await callback.message.answer(
            "🔔 Уведомления включены" if enabled else "🔕 Уведомления отключены",
            reply_markup=_limit_alerts_keyboard(enabled),
        )
    await callback.answer()


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


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Show the command index. Doubles as the entry-point for
    re-installing the persistent main menu if the user managed to
    dismiss it. Adds a hint when the client has no bots yet."""
    user = message.from_user
    if user is None:
        return
    is_admin_user = is_admin(user.id)

    lines = [
        "📋 ArmyBots — фабрика Telegram-ботов",
        "",
        "🤖 /mybots — список ваших ботов",
        "💬 /chat — пообщаться с ИИ",
        "📚 /teach — загрузить знания для бота",
        "📊 /usage — статистика токенов",
        "⚙️ /settings — настройки бота",
        "💎 /subscribe — выбор тарифа",
        "➕ /start — создать нового бота",
    ]

    client = await get_or_create_client(user.id, user.username)
    bots = await get_client_bots(client.id)
    if not bots:
        lines.extend(
            [
                "",
                "У вас пока нет ботов. Нажмите ➕ Создать бота чтобы начать.",
            ]
        )
    else:
        lines.extend(
            ["", "Используйте кнопки внизу или вводите команды."]
        )

    await message.answer(
        "\n".join(lines),
        reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
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
CRITICAL_TOKENS_THRESHOLD = 0.1
RAG_TOP_K = 3


def _tokens_left_fraction(stats: dict) -> float | None:
    """Fraction of tokens remaining (0.0-1.0). None means unlimited."""
    limit = stats.get("tokens_limit")
    if limit is None or limit <= 0:
        return None
    used = stats.get("tokens_used") or 0
    return max(0.0, (limit - used) / limit)


def _check_chat_allowed(stats: dict) -> tuple[bool, str | None]:
    """Return (allowed, reason). When allowed is False, reason is the user
    message explaining why the chat call is blocked."""
    tier = stats.get("tier")
    if tier is None:
        return False, (
            "🔒 У вас нет активной подписки. /subscribe для доступа к чату"
        )
    limit = stats.get("tokens_limit")
    if limit is None:
        return True, None
    used = stats.get("tokens_used") or 0
    if used >= limit:
        return False, "🔒 Токены закончились. /subscribe для продолжения"
    return True, None


async def _enter_chat_session(
    client_id: int,
    state: FSMContext,
    answer,
    telegram_id: int | None = None,
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

    if is_admin(telegram_id):
        return

    stats = await get_usage_stats(client_id)
    frac = _tokens_left_fraction(stats)
    if frac is not None and frac < LOW_TOKENS_THRESHOLD:
        limit = stats["tokens_limit"]
        used = stats["tokens_used"] or 0
        left = max(0, limit - used)
        await answer(
            f"⚠️ У вас осталось мало токенов ({_format_num(left)}).\n"
            "Для бесперебойной работы рекомендуем апгрейд → /subscribe"
        )


@router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    await _enter_chat_session(client.id, state, message.answer, user.id)


@router.callback_query(F.data == "post_create:chat")
async def cb_post_create_chat(
    callback: CallbackQuery, state: FSMContext
) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    await _enter_chat_session(
        client.id, state, callback.message.answer, user.id
    )
    await callback.answer()


@router.callback_query(F.data == "post_create:mybots")
async def cb_post_create_mybots(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer(
            "Управление ботами: /mybots"
        )
    await callback.answer()


_BOT_TYPE_RU: dict[str, str] = {
    "parser": "Парсер",
    "seller": "Продавец",
    "content": "Контент",
    "support": "Поддержка",
}


def _bot_type_ru(bot_type: str) -> str:
    return _BOT_TYPE_RU.get(bot_type, bot_type)


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """forms = (for 1, for 2-4, for 5-20)."""
    mod100 = abs(n) % 100
    if 11 <= mod100 <= 19:
        return forms[2]
    mod10 = mod100 % 10
    if mod10 == 1:
        return forms[0]
    if 2 <= mod10 <= 4:
        return forms[1]
    return forms[2]


def _format_relative_ru(dt: datetime | None) -> str:
    if dt is None:
        return "никогда"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 60:
        return "только что"
    if delta < 3600:
        m = int(delta // 60)
        return f"{m} {_ru_plural(m, ('минуту', 'минуты', 'минут'))} назад"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} {_ru_plural(h, ('час', 'часа', 'часов'))} назад"
    if delta < 604800:
        d = int(delta // 86400)
        return f"{d} {_ru_plural(d, ('день', 'дня', 'дней'))} назад"
    return _format_ru_date(dt)


def _bot_status_badge(bot) -> str:
    return "✅ активен" if bot.status == "active" else "⏸ на паузе"


def _mybots_keyboard(bots: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"⚙️ {b.bot_name} — управление",
                callback_data=f"bot:manage:{b.id}",
            )
        ]
        for b in bots
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_mybots_list(client_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    bots = await get_client_bots(client_id)
    if not bots:
        return "У вас нет ботов. /start чтобы создать", None

    lines = ["🤖 Ваши боты:\n"]
    for i, b in enumerate(bots, 1):
        req_count = 0
        stats = await get_bot_stats(b.id, client_id)
        if stats is not None:
            req_count = stats["request_count"]
        lines.append(
            f"{i}. {b.bot_name} ({_bot_type_ru(b.bot_type)})\n"
            f"   Статус: {_bot_status_badge(b)}\n"
            f"   Создан: {_format_ru_date(b.created_at)}\n"
            f"   Запросов: {_format_num(req_count)}"
        )
    return "\n\n".join(lines), _mybots_keyboard(bots)


@router.message(Command("mybots"))
async def cmd_mybots(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    text, kb = await _render_mybots_list(client.id)
    await message.answer(text, reply_markup=kb)


def _bot_detail_keyboard(bot) -> InlineKeyboardMarkup:
    pause_btn = (
        InlineKeyboardButton(
            text="▶️ Возобновить",
            callback_data=f"bot:resume:{bot.id}",
        )
        if bot.status == "paused"
        else InlineKeyboardButton(
            text="⏸ Пауза",
            callback_data=f"bot:pause:{bot.id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать",
                    callback_data=f"bot:edit:{bot.id}",
                ),
                pause_btn,
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"bot:delete:{bot.id}",
                ),
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="bot:list",
                ),
            ],
        ]
    )


_CONTAINER_STATUS_RU = {
    "running": "🟢 работает",
    "stopped": "🟡 остановлен",
    "not_deployed": "⚪ не развёрнут",
    "error": "🔴 ошибка",
}


def _render_bot_detail(bot, stats: dict, container_status: str) -> str:
    status_line = _CONTAINER_STATUS_RU.get(container_status, container_status)
    return (
        f"🤖 {bot.bot_name} — {_bot_type_ru(bot.bot_type)}\n"
        f"Статус: {_bot_status_badge(bot)}\n"
        f"🐳 Контейнер: {status_line}\n\n"
        "📊 Статистика:\n"
        f"• Всего запросов: {_format_num(stats['request_count'])}\n"
        f"• Токенов использовано: {_format_num(stats['tokens_used'])}\n"
        f"• Средняя длина ответа: {_format_num(stats['avg_reply_len'])} символов\n"
        f"• Последняя активность: {_format_relative_ru(stats['last_activity'])}\n\n"
        "💾 База знаний:\n"
        f"• {_format_num(stats['kb_chunks'])} "
        f"{_ru_plural(stats['kb_chunks'], ('фрагмент', 'фрагмента', 'фрагментов'))} "
        f"из {_format_num(stats['kb_sources'])} "
        f"{_ru_plural(stats['kb_sources'], ('источника', 'источников', 'источников'))}\n\n"
        "⚙️ Действия:"
    )


async def _answer_bot_detail(callback: CallbackQuery, client_id: int, bot_id: int) -> None:
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    stats = await get_bot_stats(bot_id, client_id)
    if bot_cfg is None or stats is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    container_status = await get_bot_status(bot_id)
    if callback.message is not None:
        await callback.message.answer(
            _render_bot_detail(bot_cfg, stats, container_status),
            reply_markup=_bot_detail_keyboard(bot_cfg),
        )


@router.callback_query(F.data.startswith("bot:manage:"))
async def cb_bot_manage(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    await _answer_bot_detail(callback, client.id, bot_id)
    await callback.answer()


@router.callback_query(F.data == "bot:list")
async def cb_bot_list(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    text, kb = await _render_mybots_list(client.id)
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bot:pause:"))
async def cb_bot_pause(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await set_bot_status(bot_id, client.id, "paused")
    if not ok:
        await callback.answer("Бот не найден", show_alert=True)
        return
    try:
        await stop_bot(bot_id)
    except Exception:
        logger.exception("mybots: stop_bot failed bot_id={}", bot_id)
    if callback.message is not None:
        await callback.message.answer(
            "⏸ Бот поставлен на паузу.\n"
            "Контейнер остановлен, новые запросы не обрабатываются.\n"
            "/mybots чтобы возобновить"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("bot:resume:"))
async def cb_bot_resume(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await set_bot_status(bot_id, client.id, "active")
    if not ok:
        await callback.answer("Бот не найден", show_alert=True)
        return
    # deploy_bot is idempotent — fast-starts an existing stopped container
    # or rebuilds from scratch if it was removed.
    try:
        await deploy_bot(bot_id)
    except Exception:
        logger.exception("mybots: deploy_bot failed bot_id={}", bot_id)
        if callback.message is not None:
            await callback.message.answer(
                "⚠️ Статус в БД обновлён на «активен», но контейнер не стартовал. "
                "Посмотрите логи в карточке бота."
            )
        await callback.answer()
        return
    if callback.message is not None:
        await callback.message.answer("✅ Бот снова работает")
    await callback.answer()


_EDIT_STYLES = (
    ("official", "Официальный"),
    ("friendly", "Дружелюбный"),
    ("expert", "Экспертный"),
    ("buddy", "Как друг"),
)


def _edit_menu_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Системный промпт",
                    callback_data=f"bot:edit_prompt:{bot_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎨 Имя и стиль",
                    callback_data=f"bot:edit_style:{bot_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Запреты",
                    callback_data=f"bot:edit_forbidden:{bot_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 Скрипты",
                    callback_data=f"bot:edit_scripts:{bot_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Приветствие",
                    callback_data=f"bot:edit_greeting:{bot_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"bot:manage:{bot_id}",
                )
            ],
        ]
    )


async def _resolve_edit_target(
    callback: CallbackQuery, data_prefix: str
) -> tuple[int, int] | None:
    """Parse bot_id from 'prefix:{id}' callback_data and verify ownership.
    Returns (bot_id, client_id) on success, None after answering the callback
    with a user-facing error on failure."""
    user = callback.from_user
    if user is None:
        await callback.answer()
        return None
    try:
        bot_id = int((callback.data or "").removeprefix(data_prefix))
    except ValueError:
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return None
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return None
    return bot_id, client.id


@router.callback_query(F.data.startswith("bot:edit:"))
async def cb_bot_edit(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if callback.message is not None and bot_cfg is not None:
        await callback.message.answer(
            f"🤖 {bot_cfg.bot_name} — что редактируем?",
            reply_markup=_edit_menu_keyboard(bot_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("bot:edit_prompt:"))
async def cb_bot_edit_prompt(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_prompt:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if callback.message is not None and bot_cfg is not None:
        current = bot_cfg.system_prompt or ""
        # Trim if very long — Telegram caps a single message at 4096 chars.
        preview = current if len(current) <= 3500 else current[:3500] + "…"
        await callback.message.answer(
            f"Текущий системный промпт:\n\n<code>{html.escape(preview)}</code>\n\n"
            "Отправьте новый текст промпта одним сообщением.",
            parse_mode="HTML",
        )
    await state.set_state(EditStates.waiting_prompt)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_prompt)
async def on_edit_prompt(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    new_prompt = (message.text or "").strip()
    if not new_prompt:
        await message.answer("Пустой текст. Пришлите новый промпт.")
        return
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_system_prompt(bot_id, client.id, new_prompt)
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    # Mirror in config_json for auditability — regeneration won't overwrite
    # user-provided prompt (it's the system_prompt column that runtime reads).
    await update_bot_config(bot_id, client.id, "system_prompt", new_prompt)
    await state.clear()
    logger.info(
        "edit: system_prompt updated client_id={} bot_id={} len={}",
        client.id,
        bot_id,
        len(new_prompt),
    )
    await message.answer("✅ Системный промпт обновлён. /mybots")
    await _redeploy_after_edit(bot_id, message)


def _edit_style_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"bot:edit_style_set:{bot_id}:{key}",
                )
            ]
            for key, label in _EDIT_STYLES
        ]
    )


@router.callback_query(F.data.startswith("bot:edit_style:"))
async def cb_bot_edit_style(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_style:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Выберите стиль общения:",
            reply_markup=_edit_style_keyboard(bot_id),
        )
    await callback.answer()


async def _check_bots_limit(
    client_id: int, telegram_id: int
) -> tuple[bool, str, int, int]:
    """Returns (allowed, plan_name, current_count, bots_limit). Admins
    bypass the check entirely (returns True with placeholder values).
    Clients without an active subscription fall back to starter limits.

    Used by both the post-consent guard in on_consent_yes (block before
    intake starts) and the post-token guard in on_bot_token (block right
    before save_bot_config). Two checks because the wait between consent
    and token submission can be minutes — a slot freed by parallel
    delete shouldn't be the difference between blocking and allowing."""
    if is_admin(telegram_id):
        return True, "Безлимит (админ)", 0, 0
    sub = await get_active_subscription(client_id)
    tier = sub.tier if sub else "starter"
    plan = PLANS[tier]
    limit = plan["bots_limit"]
    count = await count_client_bots(client_id)
    return count < limit, plan["name"], count, limit


async def _redeploy_after_edit(bot_id: int, message: Message) -> None:
    """Force a container rebuild so changes to system_prompt take effect
    in runtime. Called after edits that update BotConfig.system_prompt
    (direct prompt edit / regenerate-driven style/forbidden updates).
    Failures are logged but never raised — DB write already succeeded."""
    try:
        result = await redeploy_bot(bot_id)
    except Exception:
        logger.exception("redeploy_bot failed bot_id={}", bot_id)
        await message.answer(
            "⚠️ Изменения сохранены в БД, но автоперезапуск контейнера не "
            "сработал. /mybots → пауза → возобновить."
        )
        return
    if result == "paused-skipped":
        await message.answer(
            "ℹ️ Бот на паузе — новый промпт применится при возобновлении."
        )
    else:
        await message.answer("🔄 Контейнер пересобран, новый промпт активен.")


async def _regenerate_and_save(bot_id: int, client_id: int) -> bool:
    """Rebuild system_prompt from config_json and persist it. Returns
    False if the bot is not owned or config_json has no architecture."""
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if bot_cfg is None:
        return False
    cfg = dict(bot_cfg.config_json or {})
    if not cfg.get("architecture"):
        return False
    try:
        new_prompt = await asyncio.to_thread(regenerate_system_prompt, cfg)
    except Exception:
        logger.exception(
            "edit: regenerate failed client_id={} bot_id={}",
            client_id,
            bot_id,
        )
        return False
    await update_bot_system_prompt(bot_id, client_id, new_prompt)
    return True


@router.callback_query(F.data.startswith("bot:edit_style_set:"))
async def cb_bot_edit_style_set(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    # bot:edit_style_set:{id}:{style}
    if len(parts) != 4:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    try:
        bot_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    style_key = parts[3]
    if style_key not in {k for k, _ in _EDIT_STYLES}:
        await callback.answer("Неизвестный стиль", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(
        bot_id, client.id, "communication_style", style_key
    )
    if not ok:
        await callback.answer("Бот не найден", show_alert=True)
        return
    if callback.message is not None:
        await callback.message.answer(
            "🎨 Стиль сохранён. Регенерирую системный промпт…"
        )
    regenerated = await _regenerate_and_save(bot_id, client.id)
    if callback.message is not None:
        await callback.message.answer(
            "✅ Промпт обновлён с новым стилем. /mybots"
            if regenerated
            else "⚠️ Не удалось регенерировать промпт — стиль сохранён, "
            "но старый промпт остался. /mybots"
        )
        if regenerated:
            await _redeploy_after_edit(bot_id, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("bot:edit_forbidden:"))
async def cb_bot_edit_forbidden(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_forbidden:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите список запретных тем через запятую.\n"
            "Пример: политика, конкуренты, персональные данные"
        )
    await state.set_state(EditStates.waiting_forbidden)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_forbidden)
async def on_edit_forbidden(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустой список. Пришлите темы через запятую.")
        return
    items = [t.strip() for t in raw.split(",") if t.strip()]
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(
        bot_id, client.id, "forbidden_topics", items
    )
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    await state.clear()
    await message.answer(
        f"🚫 Сохранено {len(items)} "
        f"{_ru_plural(len(items), ('запрет', 'запрета', 'запретов'))}. "
        "Регенерирую системный промпт…"
    )
    regenerated = await _regenerate_and_save(bot_id, client.id)
    await message.answer(
        "✅ Промпт обновлён с новыми запретами. /mybots"
        if regenerated
        else "⚠️ Не удалось регенерировать промпт — запреты сохранены, "
        "но старый промпт остался. /mybots"
    )
    if regenerated:
        await _redeploy_after_edit(bot_id, message)


@router.callback_query(F.data.startswith("bot:edit_scripts:"))
async def cb_bot_edit_scripts(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_scripts:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите текст скриптов одним сообщением — как бот должен "
            "отвечать на типовые запросы."
        )
    await state.set_state(EditStates.waiting_scripts)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_scripts)
async def on_edit_scripts(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Пришлите скрипты.")
        return
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(bot_id, client.id, "scripts", text)
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    await state.clear()
    await message.answer("📜 Скрипты сохранены. /mybots")


@router.callback_query(F.data.startswith("bot:edit_greeting:"))
async def cb_bot_edit_greeting(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_greeting:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите новое приветствие одним сообщением."
        )
    await state.set_state(EditStates.waiting_greeting)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_greeting)
async def on_edit_greeting(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Пришлите приветствие.")
        return
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(bot_id, client.id, "greeting", text)
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    await state.clear()
    await message.answer("💬 Приветствие сохранено. /mybots")


@router.callback_query(F.data.startswith("bot:delete:"))
async def cb_bot_delete_ask(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    # bot:delete:{id} (first click)  vs  bot:delete_yes/no:{id} — handled by
    # other callbacks below. Here we only react to the first-click variant.
    if len(parts) != 3 or parts[1] != "delete":
        await callback.answer()
        return
    try:
        bot_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=f"bot:delete_yes:{bot_id}",
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"bot:delete_no:{bot_id}",
                ),
            ]
        ]
    )
    if callback.message is not None:
        await callback.message.answer(
            f"⚠️ Точно удалить {bot_cfg.bot_name}?\n"
            "Все знания и история будут утеряны.",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("bot:delete_yes:"))
async def cb_bot_delete_yes(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    # Tear down the container first — if we delete the DB row before the
    # container, we'd end up with an orphan container we no longer have the
    # bot_token for (it's sourced from BotConfig on every deploy).
    try:
        await remove_bot(bot_id)
    except Exception:
        logger.exception("mybots: remove_bot failed bot_id={}", bot_id)
    try:
        ok = await delete_bot(bot_id, client.id)
    except Exception:
        logger.exception(
            "mybots: delete_bot failed client_id={} bot_id={}",
            client.id,
            bot_id,
        )
        await callback.answer(
            "Не удалось удалить бота. Попробуйте позже.", show_alert=True
        )
        return
    if not ok:
        await callback.answer("Бот не найден", show_alert=True)
        return
    logger.info(
        "mybots: bot deleted client_id={} bot_id={}", client.id, bot_id
    )
    if callback.message is not None:
        await callback.message.answer("🗑 Бот удалён")
    await callback.answer()


@router.callback_query(F.data.startswith("bot:delete_no:"))
async def cb_bot_delete_no(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer("Отменено.")
    await callback.answer()


@router.message(Command("exit"), InlineChatStates.chatting)
async def cmd_exit_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Вы вышли из режима чата. /chat чтобы вернуться.")


IMAGE_TRIGGERS = (
    "нарисуй",
    "сгенерируй картинку",
    "создай изображение",
    "картинку",
    "изображение",
)

IMAGE_UNAVAILABLE_MSG = (
    "Сервис недоступен. Добавьте ключи Fusionbrain в настройки."
)


def _is_image_request(text: str) -> bool:
    lower = text.lower()
    return any(trigger in lower for trigger in IMAGE_TRIGGERS)


@router.message(Command("image"))
async def cmd_image(message: Message, state: FSMContext) -> None:
    await state.set_state(ImageStates.waiting_prompt)
    await message.answer("Опишите картинку которую хотите создать")


@router.message(ImageStates.waiting_prompt)
async def on_image_prompt(message: Message, state: FSMContext) -> None:
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("Пустой запрос. Опишите картинку.")
        return
    await state.clear()
    await message.answer("Генерирую картинку...")
    img = await image_generator.generate(prompt)
    if img is None:
        await message.answer(IMAGE_UNAVAILABLE_MSG)
        return
    await message.answer_photo(
        BufferedInputFile(img, filename="image.jpg")
    )


def _format_tokens_footer(stats: dict) -> str:
    tier = stats.get("tier")
    if tier is None:
        return ""
    limit = stats.get("tokens_limit")
    used = stats.get("tokens_used") or 0
    if limit is None:
        return "\n\n💬 Осталось: ∞ (безлимит)"
    left = max(0, limit - used)
    left_fmt = _format_num(left)
    if left == 0:
        return "\n\n🔒 Токены закончились. /subscribe для продолжения"
    frac = left / limit if limit > 0 else 0.0
    if frac < CRITICAL_TOKENS_THRESHOLD:
        return (
            f"\n\n🔴 Критично: осталось {left_fmt} токенов! "
            "Пополните → /subscribe"
        )
    if frac < LOW_TOKENS_THRESHOLD:
        pct = round(frac * 100)
        return (
            f"\n\n⚠️ Осталось: {left_fmt} токенов ({pct}%). "
            "Скоро закончатся → /subscribe"
        )
    return f"\n\n💬 Осталось: {left_fmt} токенов"


@router.message(InlineChatStates.chatting, F.text)
async def on_chat_message(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        return
    await _handle_chat_text(message, state, user, text)


async def _handle_chat_text(
    message: Message,
    state: FSMContext,
    user,
    text: str,
    transcription_prefix: str | None = None,
) -> None:
    if transcription_prefix:
        await message.answer(
            f"<i>Распознано: {html.escape(transcription_prefix)}</i>",
            parse_mode="HTML",
        )

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
    if bot_cfg.status == "paused":
        await message.answer("⏸ Бот на паузе. /mybots чтобы включить")
        return

    admin = is_admin(user.id)
    if not admin:
        stats_before = await get_usage_stats(client.id)
        allowed, reason = _check_chat_allowed(stats_before)
        if not allowed:
            await message.answer(reason or "Чат сейчас недоступен.")
            return

    history = await get_chat_history(
        client.id, bot_id, limit=CHAT_HISTORY_LIMIT
    )
    context_lines = []
    for h in history:
        prefix = "Клиент" if h["role"] == "user" else "Бот"
        context_lines.append(f"{prefix}: {h['content']}")
    context_str = "\n".join(context_lines)

    # Retrieval-augmented: pull up to 3 relevant knowledge chunks and inline
    # them into the system prompt. Failure to search (embeddings API down,
    # pgvector missing) must not break the chat — log and continue without.
    try:
        rag_chunks = await search_knowledge(
            client.id, bot_id, text, limit=RAG_TOP_K
        )
    except Exception:
        logger.exception(
            "chat: RAG search failed, continuing without kb "
            "(client_id={} bot_id={})",
            client.id,
            bot_id,
        )
        rag_chunks = []

    system_prompt = bot_cfg.system_prompt
    if rag_chunks:
        kb_block = "\n".join(f"- {c}" for c in rag_chunks)
        system_prompt = (
            f"{system_prompt}\n\n"
            f"Релевантная информация из базы знаний:\n{kb_block}"
        )

    img_task: asyncio.Task | None = None
    if _is_image_request(text):
        await message.answer("Генерирую картинку...")
        img_task = asyncio.create_task(image_generator.generate(text))

    strategy = (bot_cfg.config_json or {}).get("model_strategy", "auto")
    token_logs: list = []
    token_ctx = _token_accumulator.set(token_logs)
    try:
        if strategy == "smart":
            tier = "smart"
        elif strategy == "cheap":
            tier = "cheap"
        else:
            # Lazy import — avoids loading agents.router at module level and
            # mirrors how other agent modules reach into pipeline.
            from agents.router import choose_model

            tier = await choose_model(text, {"bot_type": bot_cfg.bot_type})
        reply = await asyncio.to_thread(
            run_bot_query, system_prompt, text, context_str, tier
        )
    except Exception:
        logger.exception(
            "chat: run_bot_query failed client_id={} bot_id={}",
            client.id,
            bot_id,
        )
        if img_task is not None:
            img_task.cancel()
        await message.answer(
            "Что-то пошло не так. Попробуйте ещё раз или /exit."
        )
        return
    finally:
        _token_accumulator.reset(token_ctx)

    tokens_total = sum(
        e["tokens_in"] + e["tokens_out"] for e in token_logs
    )
    if not admin:
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

    if admin:
        footer = "\n\n💬 Безлимит (админ)"
    else:
        stats = await get_usage_stats(client.id)
        footer = _format_tokens_footer(stats)

    if img_task is not None:
        try:
            img_bytes = await img_task
        except Exception:
            logger.exception("chat: image generation task failed")
            img_bytes = None
        if img_bytes:
            await message.answer_photo(
                BufferedInputFile(img_bytes, filename="image.jpg")
            )

    await message.answer(reply + footer)


TEACH_MAX_FILE_BYTES = 10 * 1024 * 1024


def _extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts).strip()


async def _active_bot(client_id: int):
    bots = await get_client_bots(client_id)
    active = [b for b in bots if b.is_active]
    return active[0] if active else None


@router.message(Command("teach"))
async def cmd_teach(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await _active_bot(client.id)
    if bot_cfg is None:
        await message.answer(
            "У вас нет активного бота. Создайте его командой /start"
        )
        return
    await state.set_state(TeachStates.receiving)
    await state.update_data(teach_bot_id=bot_cfg.id)
    await message.answer(
        "📚 Загрузите знания для вашего бота.\n"
        "Пришлите:\n"
        "• Текстовое сообщение (просто напишите)\n"
        "• PDF файл\n"
        "• TXT файл\n"
        "Бот будет использовать эту информацию при ответах.\n"
        "/done когда закончите."
    )


@router.message(Command("done"), TeachStates.receiving)
async def cmd_teach_done(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Готово! Ваш бот теперь использует эти знания в чате /chat"
    )


@router.message(TeachStates.receiving)
async def on_teach_message(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    data = await state.get_data()
    bot_id = data.get("teach_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия обучения потеряна. /teach чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)

    teach_bot = await get_bot_by_id(bot_id, client.id)
    if teach_bot is not None and teach_bot.status == "paused":
        await message.answer("⏸ Бот на паузе. /mybots чтобы включить")
        return

    raw_text = ""
    source = "text"

    if message.document is not None:
        doc = message.document
        fname = (doc.file_name or "file").strip()
        if doc.file_size and doc.file_size > TEACH_MAX_FILE_BYTES:
            mb = doc.file_size // (1024 * 1024)
            await message.answer(
                f"Файл слишком большой ({mb} MB). Лимит — 10 MB."
            )
            return
        source = fname[:256]
        buf = BytesIO()
        try:
            await bot.download(doc, destination=buf)
        except Exception:
            logger.exception("teach: download failed file_id={}", doc.file_id)
            await message.answer("Не удалось скачать файл. Попробуйте ещё раз.")
            return
        raw_bytes = buf.getvalue()
        lname = fname.lower()
        mime = (doc.mime_type or "").lower()
        if lname.endswith(".pdf") or mime == "application/pdf":
            try:
                raw_text = await asyncio.to_thread(_extract_pdf_text, raw_bytes)
            except Exception:
                logger.exception("teach: PDF parse failed fname={}", fname)
                await message.answer("Не удалось прочитать PDF. Проверьте файл.")
                return
        elif lname.endswith(".txt") or mime.startswith("text/"):
            try:
                raw_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    raw_text = raw_bytes.decode("cp1251")
                except UnicodeDecodeError:
                    await message.answer("Не удалось декодировать TXT файл.")
                    return
        else:
            await message.answer("Поддерживаются только PDF и TXT файлы.")
            return
    elif message.text:
        raw_text = message.text
        source = "text"
    else:
        await message.answer(
            "Пришлите текстовое сообщение, PDF или TXT. /done чтобы выйти."
        )
        return

    raw_text = raw_text.strip()
    if not raw_text:
        await message.answer("Текст пустой — нечего добавлять.")
        return

    try:
        added = await add_knowledge(client.id, bot_id, raw_text, source)
    except Exception:
        logger.exception(
            "teach: add_knowledge failed client_id={} source={}",
            client.id,
            source,
        )
        await message.answer(
            "Не удалось добавить в базу знаний. Попробуйте позже."
        )
        return

    if added == 0:
        await message.answer(
            "Не удалось разбить текст на фрагменты. Попробуйте ещё раз."
        )
        return

    total = await count_knowledge(client.id, bot_id)
    await message.answer(
        f"✅ Добавлено в базу знаний ({added} фрагментов).\n"
        f"Всего в базе: {total} фрагментов. Отправьте ещё или /done"
    )


@router.message(Command("knowledge"))
async def cmd_knowledge(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await _active_bot(client.id)
    if bot_cfg is None:
        await message.answer(
            "У вас нет активного бота. Создайте его командой /start"
        )
        return
    sources = await list_knowledge_sources(client.id, bot_cfg.id)
    if not sources:
        await message.answer(
            "📚 База знаний пуста.\n/teach чтобы добавить знания."
        )
        return
    total = sum(n for _, n in sources)
    lines = [f"📚 База знаний (бот: {bot_cfg.bot_name})\n"]
    for src, n in sources:
        lines.append(f"• {src} — {n} фрагментов")
    lines.append(f"\nВсего: {total} фрагментов")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Очистить базу знаний",
                    callback_data="kb:clear:ask",
                )
            ]
        ]
    )
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "kb:clear:ask")
async def cb_kb_clear_ask(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить всё", callback_data="kb:clear:yes"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data="kb:clear:no"
                )
            ],
        ]
    )
    await callback.message.answer(
        "Удалить всю базу знаний? Действие необратимо.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "kb:clear:no")
async def cb_kb_clear_no(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "kb:clear:yes")
async def cb_kb_clear_yes(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await _active_bot(client.id)
    bot_id = bot_cfg.id if bot_cfg else None
    try:
        deleted = await clear_knowledge(client.id, bot_id)
    except Exception:
        logger.exception(
            "kb: clear_knowledge failed client_id={}", client.id
        )
        await callback.message.answer(
            "Не удалось очистить базу. Попробуйте позже."
        )
        await callback.answer()
        return
    await callback.message.answer(f"🗑 Удалено {deleted} фрагментов.")
    await callback.answer()


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
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown() -> None:
        if not shutdown_event.is_set():
            logger.info("shutdown: signal received")
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows event loop does not implement add_signal_handler.
            # Local dev on Windows falls back to KeyboardInterrupt.
            pass

    await init_db()
    logger.info("База данных инициализирована")
    logger.info("Бот запущен")

    scheduler = start_alerts_scheduler(bot)

    polling_task = asyncio.create_task(
        dp.start_polling(bot), name="polling"
    )
    webhook_task = asyncio.create_task(
        start_webhook_server(), name="webhook"
    )
    shutdown_task = asyncio.create_task(
        shutdown_event.wait(), name="shutdown"
    )

    await asyncio.wait(
        {polling_task, webhook_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("shutdown: stopping polling")
    try:
        await dp.stop_polling()
    except Exception:
        logger.exception("shutdown: dp.stop_polling raised")

    try:
        scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("shutdown: scheduler.shutdown raised")

    for task in (polling_task, webhook_task, shutdown_task):
        if not task.done():
            task.cancel()
    for task in (polling_task, webhook_task, shutdown_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("shutdown: task raised during cancel")

    logger.info("shutdown: complete")


if __name__ == "__main__":
    asyncio.run(main())
