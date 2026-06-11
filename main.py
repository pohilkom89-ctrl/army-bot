import asyncio
import csv
import html
import os
import re
import signal
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
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
from config import BUSINESS_SOFT_CAP, MODELS, MODEL_STRATEGIES, PLANS, is_admin
from db.database import get_session, init_db  # noqa: F401
from bot_templates import STANDARD_BOT_CODE, TEMPLATES, get_template
from deployer import (
    clone_bot_files,
    deploy_bot,
    deploy_vk_bot,
    redeploy_bot,
    get_bot_logs,
    get_bot_status,
    prepare_bot_files,
    prepare_vk_bot_files,
    remove_bot,
    stop_bot,
    write_bot_greeting,
    write_bot_blacklist,
    write_bot_webhook_url,
    write_bot_triggers,
    write_bot_rate_limit,
    write_bot_quick_replies,
)
from db.repository import (
    anonymize_user,
    check_consent,
    count_client_bots,
    count_combo_bots,
    count_simple_bots,
    delete_bot,
    get_active_subscription,
    get_admin_stats,
    get_bot_analytics,
    get_bot_by_id,
    get_bot_stats,
    get_chat_history,
    get_client_bots,
    get_client_summary,
    get_daily_usage,
    get_limit_alerts_enabled,
    get_or_create_client,
    get_usage_by_bot,
    get_usage_by_model,
    get_usage_stats,
    get_usage_trend,
    log_tokens,
    mark_bots_merged,
    revoke_consent,
    save_bot_config,
    save_chat_message,
    save_consent,
    set_bot_status,
    set_limit_alerts,
    count_subscribers,
    get_subscriber_ids,
    rename_bot,
    update_bot_config,
    update_bot_system_prompt,
    activate_trial,
    clone_bot_config,
    get_or_create_referral_code,
    find_client_by_referral_code,
    set_referred_by,
    get_referral_stats,
    create_scheduled_broadcast,
    get_bot_scheduled_broadcasts,
    cancel_scheduled_broadcast,
    get_subscriber_stats,
    get_blacklist,
    add_to_blacklist,
    remove_from_blacklist,
    get_subscribers_for_export,
    get_bot_recent_conversations,
    get_triggers,
    set_trigger,
    remove_trigger,
    get_quick_replies,
    set_quick_replies,
)
from pipeline import (
    _token_accumulator,
    merge_bots_prompt,
    regenerate_system_prompt,
    run_bot_query,
    run_pipeline,
)
from monitoring.alerts import attach_health_monitor
from services.alerts import start_alerts_scheduler
from services.broadcasts import attach_broadcasts_scheduler
from services.image_generation import image_generator
from services.rag import (
    add_knowledge,
    clear_knowledge,
    count_knowledge,
    list_knowledge_sources,
    search_knowledge,
)
from settings import settings
from templates.bot_questionnaires import QUESTIONNAIRES, is_sensitive_question
from webhook_server import start_webhook_server

def _build_consent_text() -> str:
    parts = [
        "Для создания бота мы обрабатываем ваш Telegram ID и username.",
        "Данные хранятся на серверах в России (PostgreSQL, Redis).",
        "",
        "Для работы сервиса используются:",
        "• Telegram API — передача всех сообщений для доставки вам",
        "• OpenRouter (США) — генерация AI-ответов (передаются: технические данные бота и история ваших диалогов с ботом). Не вводите в чате персональные данные третьих лиц.",
        "• FusionBrain — генерация изображений (текстовые промпты)",
        "",
        "Вы можете удалить свои данные командой /delete_my_data",
        "Просмотр ваших данных: /my_data",
        "Отозвать согласие: /revoke_consent",
    ]
    if settings.privacy_policy_url:
        parts.append(f"Политика обработки ПДн: {settings.privacy_policy_url}")
    if settings.terms_url:
        parts.append(f"Условия использования: {settings.terms_url}")
    parts.extend(["", "Нажмите Согласен чтобы продолжить."])
    return "\n".join(parts)


CONSENT_TEXT = _build_consent_text()

# Commands that work even after consent is revoked (GDPR + re-consent flow).
_CONSENT_EXEMPT = {"/start", "/revoke_consent", "/delete_my_data", "/my_data", "/help"}


class ConsentGateMiddleware(BaseMiddleware):
    """Block users who have revoked consent from all non-exempt actions."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            user = event.from_user
            text = event.text or ""
            if text.startswith("/"):
                cmd = text.split()[0].split("@")[0]
                if cmd in _CONSENT_EXEMPT:
                    return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        else:
            return await handler(event, data)

        if user is None or is_admin(user.id):
            return await handler(event, data)

        has_consent = await check_consent(user.id)
        if not has_consent:
            notice = (
                "⚠️ Для использования сервиса необходимо согласие на обработку "
                "персональных данных.\n\nНажмите /start чтобы дать согласие."
            )
            if isinstance(event, Message):
                await event.answer(notice)
            elif isinstance(event, CallbackQuery):
                await event.answer(notice, show_alert=True)
            return

        return await handler(event, data)


class IntakeStates(StatesGroup):
    consent = State()
    ask_type = State()        # single-type (starter)
    ask_type_multi = State()  # multi-type toggle (pro/business)
    ask_vk_token = State()    # VK community token (collected before questionnaire)
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
    waiting_rename = State()
    waiting_webhook = State()


class PaymentStates(StatesGroup):
    confirm_offer = State()


class MergeStates(StatesGroup):
    selecting = State()
    naming = State()


class BroadcastStates(StatesGroup):
    confirm = State()
    sending = State()


class BlacklistStates(StatesGroup):
    waiting_id = State()


class TriggerStates(StatesGroup):
    waiting_keyword = State()
    waiting_response = State()


class RateLimitStates(StatesGroup):
    waiting_limit = State()


class QuickReplyStates(StatesGroup):
    waiting_button = State()


class CloneStates(StatesGroup):
    waiting_token = State()


class TemplateStates(StatesGroup):
    choosing = State()
    waiting_token = State()


class ScheduledBroadcastStates(StatesGroup):
    waiting_text = State()
    waiting_time = State()


# Moscow UTC+3 — used for parsing user-supplied schedule times.
_MOSCOW_TZ = timezone(timedelta(hours=3))


def _parse_schedule_time(text: str) -> datetime | None:
    """Parse schedule time from user input. Returns UTC datetime or None."""
    text = text.strip()
    # DD.MM.YYYY HH:MM
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        day, month, year, hour, minute = (int(x) for x in m.groups())
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=_MOSCOW_TZ)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    # DD.MM HH:MM — current or next year
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        day, month, hour, minute = (int(x) for x in m.groups())
        now_msk = datetime.now(_MOSCOW_TZ)
        try:
            dt = datetime(now_msk.year, month, day, hour, minute, tzinfo=_MOSCOW_TZ)
            if dt <= now_msk:
                dt = dt.replace(year=now_msk.year + 1)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


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
    rows.append([InlineKeyboardButton(
        text="🔵 Создать VK-бота (ВКонтакте)",
        callback_data="vk:start",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_question(q: dict, idx: int, total: int) -> str:
    hint = f"\n💡 {q['hint']}" if q.get("hint") else ""
    return f"Вопрос {idx}/{total}\n\n{q['text']}{hint}"


def _bot_type_multiselect_keyboard(
    selected: list[str], limit: int
) -> InlineKeyboardMarkup:
    rows = []
    for key, spec in QUESTIONNAIRES.items():
        mark = "✅" if key in selected else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {spec['name']} — {spec['description']}",
            callback_data=f"btype_m:{key}",
        )])
    if selected:
        n = len(selected)
        label = _ru_plural(n, ("тип", "типа", "типов"))
        rows.append([InlineKeyboardButton(
            text=f"Продолжить ({n} {label}) →",
            callback_data="btype_m:confirm",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _start_questionnaire(
    message: Message | None,
    state: FSMContext,
    all_types: list[str],
) -> None:
    """Kick off the answering FSM for the first type in all_types."""
    first_type = all_types[0]
    pending = all_types[1:]
    spec = QUESTIONNAIRES[first_type]
    questions = spec["questions"]

    await state.update_data(
        bot_type=first_type,
        questionnaire_type=first_type,
        selected_types=all_types,
        pending_types=pending,
        completed_answers={},
        answers={},
        current_q=0,
        total_q=len(questions),
    )
    await state.set_state(IntakeStates.answering)

    if message is None:
        return
    total = len(all_types)
    if total > 1:
        header = (
            f"━━━━━━━━━━━━━━━━━\n"
            f"📋 Анкета 1 из {total}: {spec['name']}\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
        )
    else:
        header = f"Отлично, собираем «{spec['name']}». "
    await message.answer(
        f"{header}Задам {len(questions)} вопросов — отвечайте коротко и по делу."
    )
    await message.answer(_format_question(questions[0], 1, len(questions)))


router = Router()


async def _require_consent(message: Message) -> bool:
    """Check if user has consent. If not, show warning and return False.
    Commands should check this and return early if False."""
    user = message.from_user
    if not user:
        return False

    has_consent = await check_consent(user.id)
    if not has_consent:
        await message.answer(
            "⚠️ Вы отозвали согласие на обработку персональных данных.\n\n"
            "Для продолжения работы с ботом необходимо дать согласие снова.\n"
            "Используйте /start для повторного согласия.\n\n"
            "Если вы хотите удалить свои данные, используйте /delete_my_data"
        )
        return False
    return True


# Persistent main menu shown after consent. 4×2 layout. Admins see
# "💎 Безлимит (админ)" instead of "💎 Тариф" — they have an unlimited
# tier and don't need /subscribe.
_MAIN_MENU_BUTTONS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("💬 Чат с ботом", "/chat"), ("🤖 Мои боты", "/mybots")),
    (("📊 Статистика", "/usage"), ("📚 Обучение", "/teach")),
    (("⚙️ Настройки", "/settings"), ("💎 Тариф", "/subscribe")),
    (("➕ Создать бота", "/start"), ("📋 Шаблоны", "/templates")),
    (("❓ Помощь", "/help"),),
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

    # Check consent for all menu actions except /start and /help
    if label not in ("➕ Создать бота", "❓ Помощь"):
        if not await _require_consent(message):
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

    logger.info("intake: /start from tg_id={}", user.id)
    await state.clear()
    client = await get_or_create_client(user.id, user.username)

    # Deep-link referral: /start ref_<code>
    args = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else ""
    if args.startswith("ref_"):
        ref_code = args[4:]
        referrer = await find_client_by_referral_code(ref_code)
        if referrer is not None and referrer.id != client.id:
            linked = await set_referred_by(client.id, referrer.id)
            if linked:
                logger.info(
                    "referral: tg_id={} referred by client_id={}", user.id, referrer.id
                )

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

    trial_activated = await activate_trial(client.id)
    if trial_activated:
        await message.answer(
            "🎁 Вам активирован пробный период 7 дней на тарифе Про — бесплатно!\n"
            "Все функции доступны без ограничений. Подписку можно оформить в /subscribe."
        )

    allowed, plan_name, count, limit = await _check_bots_limit(client.id, user.id)
    if not allowed:
        await state.clear()
        await message.answer(
            f"⚠️ Все слоты заняты на тарифе {plan_name} ({count}/{limit} ботов).\n"
            "Создание нового невозможно — удалите существующего "
            "через 🤖 Мои боты или апгрейд тарифа через 💎 Тариф.",
            reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
        )
        return

    # Install the persistent main menu now that the user has consented.
    await message.answer(
        "Согласие сохранено. Меню всегда доступно внизу экрана.",
        reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
    )

    sub = await get_active_subscription(client.id)
    tier = sub.tier if (sub and sub.status == "active") else "starter"
    multitype_limit: int = PLANS[tier].get("multitype_limit", 1)

    if multitype_limit > 1:
        await state.set_state(IntakeStates.ask_type_multi)
        await state.update_data(multitype_limit=multitype_limit, selected_types=[])
        await message.answer(
            f"Что именно вам нужно?\n"
            f"На тарифе {PLANS[tier]['name']} можно объединить до {multitype_limit} типов в одном боте:",
            reply_markup=_bot_type_multiselect_keyboard([], multitype_limit),
        )
    else:
        await state.set_state(IntakeStates.ask_type)
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
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await _start_questionnaire(callback.message, state, [key])
    await callback.answer()


@router.callback_query(IntakeStates.ask_type_multi, F.data.startswith("btype_m:"))
async def cb_btype_multi(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_types") or [])
    limit: int = data.get("multitype_limit", 2)

    if key == "confirm":
        if not selected:
            await callback.answer("Выберите хотя бы один тип", show_alert=True)
            return
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
        await _start_questionnaire(callback.message, state, selected)
        await callback.answer()
        return

    if key not in QUESTIONNAIRES:
        await callback.answer("Неизвестный тип", show_alert=True)
        return

    if key in selected:
        selected.remove(key)
    elif len(selected) < limit:
        selected.append(key)
    else:
        await callback.answer(
            f"Максимум {limit} типа на вашем тарифе", show_alert=True
        )
        return

    await state.update_data(selected_types=selected)
    if callback.message is not None:
        await callback.message.edit_reply_markup(
            reply_markup=_bot_type_multiselect_keyboard(selected, limit)
        )
    await callback.answer()


# --- VK bot creation flow ---

@router.callback_query(IntakeStates.ask_type, F.data == "vk:start")
async def cb_vk_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(platform="vk")
    await state.set_state(IntakeStates.ask_vk_token)
    await callback.message.answer(
        "Отлично! Для VK-бота нужен токен сообщества.\n\n"
        "Перейдите в настройки своего VK-сообщества → Работа с API → "
        "Создайте ключ доступа с правами: сообщения, фотографии.\n\n"
        "Отправьте токен сообщества:"
    )
    await callback.answer()


async def _validate_vk_token(token: str) -> bool:
    """Call VK API groups.getById to verify the community token is valid."""
    import aiohttp
    url = "https://api.vk.com/method/groups.getById"
    params = {"access_token": token, "v": "5.131"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return "response" in data
    except Exception:
        return False


@router.message(IntakeStates.ask_vk_token)
async def on_vk_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("Пожалуйста, отправьте токен сообщества ВКонтакте.")
        return

    await message.answer("Проверяю токен...")
    valid = await _validate_vk_token(token)
    if not valid:
        await message.answer(
            "Токен недействителен или у него нет нужных прав.\n"
            "Проверьте токен и отправьте ещё раз."
        )
        return

    await state.update_data(vk_token=token)
    await state.set_state(IntakeStates.ask_type)
    await message.answer(
        "Токен VK-сообщества принят!\n\n"
        "Теперь выберите тип бота:",
        reply_markup=_bot_type_keyboard_vk(),
    )


def _bot_type_keyboard_vk() -> InlineKeyboardMarkup:
    """Type selection for VK bots — same types but no VK entry point."""
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
        "sensitive": is_sensitive_question(current_q["text"], current_q.get("hint", "")),
    }

    next_idx = idx + 1
    await state.update_data(answers=answers, current_q=next_idx)

    if next_idx < len(questions):
        next_q = questions[next_idx]
        await message.answer(
            _format_question(next_q, next_idx + 1, len(questions))
        )
        return

    # Save this type's answers and check if more types remain.
    completed = dict(data.get("completed_answers") or {})
    completed[bot_type] = answers
    pending = list(data.get("pending_types") or [])

    if pending:
        next_type = pending.pop(0)
        spec_next = QUESTIONNAIRES[next_type]
        questions_next = spec_next["questions"]
        selected_types: list[str] = data.get("selected_types") or [bot_type]
        type_num = len(selected_types) - len(pending)
        await state.update_data(
            bot_type=next_type,
            questionnaire_type=next_type,
            answers={},
            current_q=0,
            total_q=len(questions_next),
            pending_types=pending,
            completed_answers=completed,
        )
        await message.answer(
            f"━━━━━━━━━━━━━━━━━\n"
            f"📋 Анкета {type_num} из {len(selected_types)}: {spec_next['name']}\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"Задам {len(questions_next)} вопросов — продолжайте в том же духе."
        )
        await message.answer(_format_question(questions_next[0], 1, len(questions_next)))
        return

    await state.update_data(completed_answers=completed)

    # Multi-type: skip clarification, go directly to token (or process for VK).
    selected_types = data.get("selected_types") or [bot_type]
    if len(selected_types) > 1:
        await _goto_token_or_process(message, state)
        return

    # Single-type: existing clarification flow.
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

    await _goto_token_or_process(message, state)


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

    await _goto_token_or_process(message, state)


async def _goto_token_or_process(message: Message, state: FSMContext) -> None:
    """After questionnaire: ask Telegram token or run VK pipeline directly."""
    data = await state.get_data()
    if data.get("platform") == "vk":
        await state.set_state(IntakeStates.processing)
        await _run_pipeline_and_save(message, state, data["vk_token"], platform="vk")
    else:
        await state.set_state(IntakeStates.ask_bot_token)
        await message.answer(ASK_TOKEN_PROMPT)


async def _run_pipeline_and_save(
    message: Message,
    state: FSMContext,
    bot_token: str,
    platform: str = "telegram",
) -> None:
    """Run pipeline, save bot config, prepare files, and deploy."""
    user = message.from_user
    data = await state.get_data()
    bot_type = data.get("bot_type")
    raw_answers: dict = data.get("answers") or {}
    clarification_answers: dict = data.get("clarification_answers") or {}
    selected_types: list[str] = data.get("selected_types") or []
    completed_answers: dict = data.get("completed_answers") or {}

    if len(selected_types) > 1:
        combined_raw: dict = {}
        for type_key in selected_types:
            type_name = QUESTIONNAIRES[type_key]["name"]
            for qid, entry in completed_answers.get(type_key, {}).items():
                combined_raw[f"{type_key}_{qid}"] = {
                    **entry,
                    "question": f"[{type_name}] {entry['question']}",
                }
        llm_answers, sensitive_count = _redact_sensitive(combined_raw)
        pipeline_input = {
            "bot_type": "merged",
            "questionnaire_type": "merged",
            "merged_types": selected_types,
            "answers": llm_answers,
            "clarification_answers": {},
        }
        raw_answers_to_save = combined_raw
        final_type = "merged"
    else:
        llm_answers, sensitive_count = _redact_sensitive(raw_answers)
        pipeline_input = {
            "bot_type": bot_type,
            "questionnaire_type": bot_type,
            "answers": llm_answers,
            "clarification_answers": clarification_answers,
        }
        raw_answers_to_save = raw_answers
        final_type = None

    await message.answer("Агенты приступили к работе, ожидайте ~60 секунд...")

    logger.info(
        "intake: pipeline launched for tg_id={} bot_type={} q_count={} sensitive={} platform={}",
        user.id,
        bot_type,
        len(llm_answers),
        sensitive_count,
        platform,
    )
    try:
        spec = await asyncio.to_thread(run_pipeline, pipeline_input)
        client = await get_or_create_client(user.id, user.username)

        # Second bots-limit gate: between consent and token submission a
        # parallel session could have created bots, or the client could
        # have used multiple devices to start two intakes at once. Pipeline
        # has already run (tokens spent) — log warning if we have to bail.
        _is_combo = len(selected_types) > 1
        allowed, plan_name, count, limit = await _check_bots_limit(
            client.id, user.id, is_combo=_is_combo
        )
        if not allowed:
            _kind = "комбо-ботов" if _is_combo else "простых ботов"
            logger.warning(
                "intake: bots_limit hit AFTER pipeline for client_id={} "
                "(tokens already spent, count={}/{} {} on {})",
                client.id,
                count,
                limit,
                _kind,
                plan_name,
            )
            await message.answer(
                f"🚫 Достигнут лимит {_kind} на вашем тарифе.\n\n"
                f"Текущий тариф: {plan_name} ({_kind}: {count}/{limit})\n\n"
                "Варианты:\n"
                "• Удалите ненужного бота через 🤖 Мои боты\n"
                "• Перейдите на тариф выше → 💎 Тариф",
                reply_markup=_main_menu_keyboard(is_admin_user=is_admin(user.id)),
            )
            await state.clear()
            return

        resolved_type = final_type or spec.requirements.get("bot_type", bot_type or "other")
        config_extra = (
            {"merged_types": selected_types} if len(selected_types) > 1 else {}
        )
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
                "questionnaire_answers": raw_answers_to_save,
                "clarification_answers": clarification_answers,
                **config_extra,
            },
            bot_token=bot_token,
            platform=platform,
        )
        if platform == "vk":
            prepare_vk_bot_files(saved_bot.id, spec.system_prompt)
        else:
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
        "intake: pipeline ok for client_id={} bot_id={} platform={}",
        client.id,
        saved_bot.id,
        platform,
    )

    deploy_ok = False
    try:
        if platform == "vk":
            await deploy_vk_bot(saved_bot.id)
        else:
            await deploy_bot(saved_bot.id)
        deploy_ok = True
    except Exception:
        logger.exception(
            "intake: deploy failed for bot_id={} platform={}", saved_bot.id, platform
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
                    text="🤖 Карточка бота",
                    callback_data=f"post_create:mybots:{saved_bot.id}",
                )
            ],
        ]
    )
    platform_label = "VK" if platform == "vk" else "Telegram"
    if deploy_ok:
        await message.answer(
            f"✅ {platform_label}-бот готов и запущен в контейнере!\n\nТип: {resolved_type}\n"
            f"Контейнер: bot_client_{saved_bot.id}\n\n"
            "Оформите подписку /subscribe чтобы открыть доступ клиентам.",
            reply_markup=post_create_kb,
        )
    else:
        await message.answer(
            f"⚠️ {platform_label}-бот создан, но контейнер не поднялся.\n\nТип: {resolved_type}\n\n"
            "Напишите в поддержку: вероятно конфликт токена или ошибка в коде бота. "
            "Можно посмотреть логи через /mybots → карточка бота.",
            reply_markup=post_create_kb,
        )
    await state.clear()


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

    await state.set_state(IntakeStates.processing)
    await _run_pipeline_and_save(message, state, bot_token, platform="telegram")


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

    plan = stats.get("plan")
    tier_label = PLANS[tier]["name"] if tier in PLANS else tier
    reset_short = _format_ru_date_short(reset_at) if reset_at else "—"
    days_to_reset = _days_until(reset_at)

    if plan == "trial":
        tier_label = (
            f"🎁 Пробный период — {tier_label} "
            f"(осталось {days_to_reset} "
            f"{_ru_plural(days_to_reset, ('день', 'дня', 'дней'))})"
        )
        reset_line = f"Истекает: {reset_short}"
        if days_to_reset <= 1:
            reset_line = f"⚠️ Истекает сегодня! {reset_short} → /subscribe"
    else:
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

    bar, pct_used = _progress_bar_used(used, limit) if limit else ("██████████", 0)
    if limit is None:
        total_line = (
            f"Всего: {_format_num(used)} токенов\n"
            "██████████ ∞ безлимит\n"
        )
    elif tier == "business":
        total_line = (
            f"Всего: {_format_num(used)} / {_format_num(limit)} токенов\n"
            f"{bar} {pct_used}%  (мягкий лимит, далее — кастомный тариф)\n"
        )
    else:
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
    if not await _require_consent(message):
        return

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
    if not await _require_consent(message):
        return

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
    if not await _require_consent(message):
        return

    await message.answer(
        "Выберите тариф подписки:", reply_markup=_subscribe_keyboard()
    )


def _templates_list_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{t['emoji']} {t['name']} — {t['description']}",
                callback_data=f"tpl:preview:{key}",
            )
        ]
        for key, t in TEMPLATES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("templates"))
async def cmd_templates(message: Message, state: FSMContext) -> None:
    if not await _require_consent(message):
        return
    await state.set_state(TemplateStates.choosing)
    await message.answer(
        "📋 <b>Шаблоны ботов</b>\n\n"
        "Готовые боты — никакого пайплайна, запуск за ~10 секунд.\n"
        "Промпт можно отредактировать через /mybots после создания.\n\n"
        "Выберите шаблон:",
        parse_mode="HTML",
        reply_markup=_templates_list_keyboard(),
    )


@router.callback_query(F.data.startswith("tpl:preview:"), TemplateStates.choosing)
async def cb_template_preview(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    key = (callback.data or "").split(":")[2]
    tmpl = get_template(key)
    if tmpl is None:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Выбрать этот шаблон",
                    callback_data=f"tpl:choose:{key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ К списку",
                    callback_data="tpl:list",
                )
            ],
        ]
    )
    await callback.message.answer(tmpl["preview"], reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "tpl:list")
async def cb_template_list(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await state.set_state(TemplateStates.choosing)
    await callback.message.answer(
        "Выберите шаблон:", reply_markup=_templates_list_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tpl:choose:"))
async def cb_template_choose(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    key = (callback.data or "").split(":")[2]
    tmpl = get_template(key)
    if tmpl is None:
        await callback.answer("Шаблон не найден", show_alert=True)
        return

    client = await get_or_create_client(user.id, user.username)
    allowed, plan_name, count, limit = await _check_bots_limit(client.id, user.id)
    if not allowed:
        await callback.answer(
            f"Лимит ботов на тарифе {plan_name} ({count}/{limit}).\n"
            "Удалите бота или оформите подписку → /subscribe",
            show_alert=True,
        )
        return

    await state.set_state(TemplateStates.waiting_token)
    await state.update_data(template_key=key)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="tpl:cancel")
        ]]
    )
    await callback.message.answer(
        f"Отлично! Шаблон <b>{tmpl['emoji']} {tmpl['name']}</b> выбран.\n\n"
        "Создайте бота через @BotFather и отправьте его токен:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "tpl:cancel")
async def cb_template_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message is not None:
        await callback.message.answer("Создание по шаблону отменено.")
    await callback.answer()


@router.message(TemplateStates.waiting_token)
async def on_template_token(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    new_token = (message.text or "").strip()
    try:
        validate_token(new_token)
    except TokenValidationError:
        await message.answer(
            "Это не похоже на токен бота. Проверьте формат и отправьте ещё раз."
        )
        return

    data = await state.get_data()
    key = data.get("template_key")
    tmpl = get_template(key) if key else None
    if tmpl is None:
        await message.answer("Сессия устарела. Начните заново → /templates")
        await state.clear()
        return

    client = await get_or_create_client(user.id, user.username)
    await message.answer(f"Создаю {tmpl['emoji']} {tmpl['name']}...")

    try:
        bot_cfg = await save_bot_config(
            client_id=client.id,
            bot_name=f"{tmpl['emoji']} {tmpl['name']}",
            bot_type=tmpl["bot_type"],
            system_prompt=tmpl["system_prompt"],
            config={"model_strategy": "auto", "template_key": key},
            bot_token=new_token,
        )
    except Exception:
        logger.exception("templates: save_bot_config failed tg_id={}", user.id)
        await message.answer("Не удалось создать бота. Попробуйте ещё раз.")
        await state.clear()
        return

    try:
        await asyncio.to_thread(prepare_bot_files, STANDARD_BOT_CODE, bot_cfg.id)
    except Exception:
        logger.exception("templates: prepare_bot_files failed bot_id={}", bot_cfg.id)
        await message.answer("Ошибка при подготовке файлов. Попробуйте ещё раз.")
        await state.clear()
        return

    deploy_ok = False
    try:
        await deploy_bot(bot_cfg.id)
        deploy_ok = True
    except Exception:
        logger.exception("templates: deploy_bot failed bot_id={}", bot_cfg.id)

    await state.clear()
    if deploy_ok:
        await message.answer(
            f"✅ {tmpl['emoji']} <b>{tmpl['name']}</b> создан и запущен!\n\n"
            "Управление → /mybots\n"
            "Промпт можно настроить: /mybots → Редактировать → ✏️ Промпт",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"⚠️ Бот создан, но контейнер не поднялся.\n"
            f"{tmpl['emoji']} {tmpl['name']}\n"
            "Проверьте токен в /mybots → перезапуск."
        )
    logger.info(
        "templates: created bot_id={} template={} client_id={}",
        bot_cfg.id, key, client.id,
    )



_ONBOARDING_PAGES = [
    (
        "🏠 ArmyBots — фабрика AI-ботов (1/6)",
        (
            "ArmyBots создаёт готового AI-бота за ~60 секунд — без кода:\n\n"
            "• Telegram или ВКонтакте\n"
            "• Персональный AI (GPT / Gemini / Qwen)\n"
            "• Системный промпт под вашу нишу\n"
            "• 14 типов ботов: поддержка, продажи, HR, коучинг, недвижимость...\n"
            "• Работает в Docker — вы просто управляете через Telegram\n\n"
            "Листайте дальше — покажу как это работает."
        ),
    ),
    (
        "🤖 Создание бота (2/6)",
        (
            "Создать бота очень просто:\n\n"
            "1️⃣ Нажмите ➕ Создать бота или /start\n"
            "2️⃣ Выберите тип бота (поддержка, продажи, HR...)\n"
            "3️⃣ Ответьте на 5–7 вопросов об вашем бизнесе\n"
            "4️⃣ Создайте бота через @BotFather → получите токен\n"
            "5️⃣ Вставьте токен — бот готов и запущен!\n\n"
            "Для VK-бота: нажмите «🔵 Создать VK-бота» и введите токен сообщества.\n\n"
            "💡 Хотите быстрее? /templates — готовые боты в 2 клика."
        ),
    ),
    (
        "⚙️ Управление ботом (3/6)",
        (
            "Все боты — в /mybots. Нажмите ✏️ Редактировать:\n\n"
            "📝 Системный промпт — суть и характер бота\n"
            "💬 Приветствие — первое сообщение подписчику\n"
            "⚡ Триггеры — авто-ответы по ключевым словам\n"
            "📋 Кнопки быстрых ответов — меню для пользователей\n"
            "🛡 Лимит сообщений — защита от спама\n"
            "🔗 Webhook — интеграция с CRM\n\n"
            "📢 Рассылки по расписанию — жмите «Рассылка» в карточке бота.\n"
            "⏸ Пауза / ▶️ Возобновить — моментальный контроль работы."
        ),
    ),
    (
        "📚 Знания и AI-чат (4/6)",
        (
            "Сделайте бота экспертом в вашей теме:\n\n"
            "📥 /teach — загрузить текст, FAQ, документы\n"
            "Бот отвечает, опираясь на вашу базу знаний (RAG).\n\n"
            "📋 /knowledge — посмотреть загруженные источники\n\n"
            "💬 /chat — протестировать бота лично перед запуском\n\n"
            "🎨 /image — сгенерировать картинку через FusionBrain\n\n"
            "🎙 Голосовые сообщения — бот транскрибирует через Whisper\n"
            "📷 Фото — бот описывает изображения через Gemini Vision"
        ),
    ),
    (
        "💎 Тарифы и пробный период (5/6)",
        (
            "Выберите тариф под ваши задачи:\n\n"
            "🟢 Starter — 490₽/мес\n"
            "   1 Telegram-бот, 1М токенов\n\n"
            "🔵 Pro — 949₽/мес\n"
            "   2 простых + 2 комбо-бота, 5М токенов\n\n"
            "🟣 Business — 2 990₽/мес\n"
            "   5+3 бота, ~50М токенов\n\n"
            "🎁 Пробный период: 7 дней Про — бесплатно\n"
            "   Активируется автоматически при первом боте!\n\n"
            "/subscribe — оформить или продлить подписку"
        ),
    ),
    (
        "🤝 Реферальная программа (6/6)",
        (
            "Приглашайте коллег — получайте бонусы!\n\n"
            "За каждого друга, который оформит подписку:\n"
            "➕ +30 дней Про добавляется к вашей подписке\n\n"
            "Количество рефералов не ограничено.\n"
            "Уведомление приходит автоматически, как только друг оплатит.\n\n"
            "/referral — получить вашу реферальную ссылку\n\n"
            "—\n"
            "Команды: /mybots /usage /settings /subscribe /chat /teach\n"
            "Данные: /my_data /delete_my_data /revoke_consent"
        ),
    ),
]

_HELP_TOTAL = len(_ONBOARDING_PAGES)


def _help_page_keyboard(page: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"help:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{_HELP_TOTAL}", callback_data="help:noop"))
    if page < _HELP_TOTAL - 1:
        nav.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"help:page:{page + 1}"))
    rows = [nav]
    if page == _HELP_TOTAL - 1:
        rows.append([InlineKeyboardButton(text="✅ Понятно, начать!", callback_data="help:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    is_admin_user = is_admin(user.id)
    client = await get_or_create_client(user.id, user.username)
    bots = await get_client_bots(client.id)
    # New users land on page 1 (how to create), returning users on page 0.
    start_page = 1 if not bots else 0
    title, body = _ONBOARDING_PAGES[start_page]
    await message.answer(
        f"<b>{title}</b>\n\n{body}",
        parse_mode="HTML",
        reply_markup=_help_page_keyboard(start_page),
    )
    await message.answer(
        "Главное меню восстановлено.",
        reply_markup=_main_menu_keyboard(is_admin_user=is_admin_user),
    )


@router.callback_query(F.data.startswith("help:page:"))
async def cb_help_page(callback: CallbackQuery) -> None:
    try:
        page = int((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    page = max(0, min(page, _HELP_TOTAL - 1))
    title, body = _ONBOARDING_PAGES[page]
    if callback.message is not None:
        await callback.message.edit_text(
            f"<b>{title}</b>\n\n{body}",
            parse_mode="HTML",
            reply_markup=_help_page_keyboard(page),
        )
    await callback.answer()


@router.callback_query(F.data == "help:noop")
async def cb_help_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "help:close")
async def cb_help_close(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Туториал закрыт. /help чтобы открыть снова.")


@router.callback_query(F.data.startswith("subscribe:"))
async def on_subscribe_choice(callback: CallbackQuery, state: FSMContext) -> None:
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

    # Save tier and cycle to FSM and ask for offer agreement
    await state.update_data(payment_tier=tier, payment_cycle=cycle)
    await state.set_state(PaymentStates.confirm_offer)

    plan_name = PLANS[tier]["name"]
    cycle_label = "месяц" if cycle == "monthly" else "год"
    price_key = "price_monthly" if cycle == "monthly" else "price_yearly"
    price = PLANS[tier][price_key]

    offer_text = (
        f"Вы выбрали тариф: {plan_name} ({price}₽/{cycle_label})\n\n"
        "Перед оплатой необходимо согласиться с условиями оферты.\n"
    )
    if settings.terms_url:
        offer_text += f"Ознакомиться с офертой: {settings.terms_url}\n\n"

    offer_text += "Согласны с условиями оферты?"

    offer_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Согласен", callback_data="offer:accept"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="offer:cancel"),
            ]
        ]
    )

    if callback.message is not None:
        await callback.message.answer(offer_text, reply_markup=offer_kb)
    await callback.answer()


@router.callback_query(F.data.startswith("offer:"), PaymentStates.confirm_offer)
async def on_offer_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None:
        return

    action = (callback.data or "").split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.answer("Оплата отменена")
        if callback.message is not None:
            await callback.message.answer("Оплата отменена. /subscribe для выбора другого тарифа")
        return

    if action != "accept":
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    # User accepted the offer, proceed with payment
    data = await state.get_data()
    tier = data.get("payment_tier")
    cycle = data.get("payment_cycle")

    if not tier or not cycle:
        await callback.answer("Ошибка: данные тарифа потеряны", show_alert=True)
        await state.clear()
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
        await state.clear()
        return

    logger.info(
        "billing: payment link sent client_id={} tier={} cycle={} (offer accepted)",
        client.id,
        tier,
        cycle,
    )
    if callback.message is not None:
        await callback.message.answer(
            f"Оплатите подписку по ссылке:\n{payment_url}"
        )
    await callback.answer()
    await state.clear()


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
    used = stats.get("tokens_used") or 0
    if limit is None or used < limit:
        return True, None
    if tier == "business":
        cap_fmt = _format_num(BUSINESS_SOFT_CAP)
        return False, (
            f"🔒 Достигнут лимит {cap_fmt} токенов на тарифе Бизнес.\n"
            "Напишите в поддержку для подключения кастомного тарифа: /help"
        )
    return False, "🔒 Токены закончились. /subscribe для продолжения"


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
        f"Отправляйте сообщения. /exit чтобы выйти.\n\n"
        f"⚠️ Внимание: не указывайте в чате персональные данные третьих лиц "
        f"(ФИО, телефоны, адреса других людей). Ваши сообщения обрабатываются AI-моделью."
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
    if not await _require_consent(message):
        return

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


@router.callback_query(F.data.startswith("post_create:mybots:"))
async def cb_post_create_mybots(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    await _answer_bot_detail(callback, client.id, bot_id)
    await callback.answer()


_BOT_TYPE_RU: dict[str, str] = {
    "parser": "Парсер",
    "seller": "Продавец",
    "content": "Контент",
    "support": "Поддержка",
    "merged": "Объединённый",
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


def _platform_icon(bot) -> str:
    return "🔵" if getattr(bot, "platform", "telegram") == "vk" else "⚙️"


def _mybots_keyboard(bots: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{_platform_icon(b)} {b.bot_name} — управление",
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
        platform = getattr(b, "platform", "telegram")
        platform_tag = " 🔵 VK" if platform == "vk" else ""
        lines.append(
            f"{i}. {b.bot_name} ({_bot_type_ru(b.bot_type)}){platform_tag}\n"
            f"   Статус: {_bot_status_badge(b)}\n"
            f"   Создан: {_format_ru_date(b.created_at)}\n"
            f"   Запросов: {_format_num(req_count)}"
        )
    return "\n\n".join(lines), _mybots_keyboard(bots)


def _merge_keyboard(
    bots: list, selected: list[int], limit: int
) -> InlineKeyboardMarkup:
    rows = []
    for b in bots:
        mark = "✅" if b.id in selected else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {b.bot_name} ({_bot_type_ru(b.bot_type)})",
            callback_data=f"merge:toggle:{b.id}",
        )])
    if len(selected) >= 2:
        n = len(selected)
        label = _ru_plural(n, ("бота", "бота", "ботов"))
        rows.append([InlineKeyboardButton(
            text=f"Объединить {n} {label} →",
            callback_data="merge:confirm",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="merge:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("merge_bots"))
async def cmd_merge_bots(message: Message, state: FSMContext) -> None:
    if not await _require_consent(message):
        return
    user = message.from_user
    if user is None:
        return
    client = await get_or_create_client(user.id, user.username)
    sub = await get_active_subscription(client.id)
    tier = sub.tier if (sub and sub.status == "active") else "starter"
    merge_limit: int = PLANS[tier].get("merge_limit", 0)

    if merge_limit < 2:
        await message.answer(
            "Объединение ботов доступно начиная с тарифа Про.\n"
            "/usage — посмотреть текущий тариф."
        )
        return

    bots = await get_client_bots(client.id)
    if len(bots) < 2:
        await message.answer(
            "Нужно минимум 2 бота для объединения. Создайте ещё через /start."
        )
        return

    await state.set_state(MergeStates.selecting)
    await state.update_data(client_id=client.id, selected=[], limit=merge_limit)
    plan_name = PLANS[tier]["name"]
    await message.answer(
        f"Выберите боты для объединения (до {merge_limit} на тарифе {plan_name}):\n"
        "Объединённый бот наследует токен первого выбранного бота.",
        reply_markup=_merge_keyboard(bots, [], merge_limit),
    )


@router.callback_query(MergeStates.selecting, F.data.startswith("merge:toggle:"))
async def cb_merge_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    bot_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected: list[int] = list(data.get("selected", []))
    limit: int = data["limit"]
    client_id: int = data["client_id"]

    if bot_id in selected:
        selected.remove(bot_id)
    elif len(selected) < limit:
        selected.append(bot_id)
    else:
        await callback.answer(
            f"Максимум {limit} бота для вашего тарифа", show_alert=True
        )
        return

    await state.update_data(selected=selected)
    bots = await get_client_bots(client_id)
    if callback.message is not None:
        await callback.message.edit_reply_markup(
            reply_markup=_merge_keyboard(bots, selected, limit)
        )
    await callback.answer()


@router.callback_query(MergeStates.selecting, F.data == "merge:confirm")
async def cb_merge_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if len(data.get("selected", [])) < 2:
        await callback.answer("Выберите минимум 2 бота", show_alert=True)
        return
    await state.set_state(MergeStates.naming)
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Введите название для объединённого бота:")
    await callback.answer()


@router.callback_query(MergeStates.selecting, F.data == "merge:cancel")
async def cb_merge_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Отменено.")
    await callback.answer()


@router.message(MergeStates.naming)
async def on_merge_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название должно быть от 1 до 64 символов.")
        return

    data = await state.get_data()
    selected_ids: list[int] = data.get("selected", [])
    client_id: int = data["client_id"]
    await state.clear()

    bots = [await get_bot_by_id(bid, client_id) for bid in selected_ids]
    bots = [b for b in bots if b is not None]
    if len(bots) < 2:
        await message.answer("Что-то пошло не так. Попробуйте снова: /merge_bots")
        return

    wait_msg = await message.answer(f"Создаю объединённого бота «{name}»...")

    try:
        system_prompt = await asyncio.to_thread(merge_bots_prompt, name, bots)
        merged = await save_bot_config(
            client_id=client_id,
            bot_type="merged",
            bot_name=name,
            system_prompt=system_prompt,
            config={"merged_from": selected_ids},
            bot_token=bots[0].bot_token,
        )
        await mark_bots_merged(selected_ids, merged.id)
        source_names = ", ".join(b.bot_name for b in bots)
        await wait_msg.edit_text(
            f"Готово! Бот «{name}» создан.\n"
            f"Объединил: {source_names}\n\n"
            "Управление: /mybots"
        )
    except Exception as exc:
        logger.error("merge_bots: failed for client_id={}: {}", client_id, exc)
        await wait_msg.edit_text(
            "Ошибка при создании объединённого бота. Попробуйте позже."
        )


@router.message(Command("mybots"))
async def cmd_mybots(message: Message) -> None:
    if not await _require_consent(message):
        return

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
                    text="📊 Аналитика",
                    callback_data=f"bot:analytics:{bot.id}",
                ),
                InlineKeyboardButton(
                    text="📢 Рассылка",
                    callback_data=f"bot:broadcast:{bot.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Диалоги",
                    callback_data=f"bot:conversations:{bot.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 Расписание",
                    callback_data=f"bot:schedule:{bot.id}",
                ),
                InlineKeyboardButton(
                    text="🔁 Клонировать",
                    callback_data=f"bot:clone:{bot.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"bot:delete:{bot.id}",
                ),
            ],
            [
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


@router.callback_query(F.data.startswith("bot:analytics:"))
async def cb_bot_analytics(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:analytics:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    analytics, sub_stats = await asyncio.gather(
        get_bot_analytics(bot_id, client_id),
        get_subscriber_stats(bot_id, client_id),
    )
    if bot_cfg is None or analytics is None:
        await callback.answer("Бот не найден", show_alert=True)
        return

    peak = analytics["peak_hour"]
    peak_str = f"{peak:02d}:00–{(peak+1)%24:02d}:00 UTC" if peak is not None else "нет данных"

    sub_section = ""
    if sub_stats:
        sub_section = (
            f"\n📣 Подписчики\n"
            f"   Всего: {_format_num(sub_stats['total'])}\n"
            f"   Сегодня: +{_format_num(sub_stats['new_today'])}\n"
            f"   За 7 дней: +{_format_num(sub_stats['new_7d'])}\n"
            f"   За 30 дней: +{_format_num(sub_stats['new_30d'])}\n"
        )

    text = (
        f"📊 Аналитика — {bot_cfg.bot_name}\n"
        f"{sub_section}\n"
        f"💬 Диалоги\n"
        f"   Уникальных пользователей: {_format_num(analytics['unique_users'])}\n"
        f"   Всего сообщений: {_format_num(analytics['total_messages'])}\n"
        f"   За 7 дней: {_format_num(analytics['messages_7d'])}\n"
        f"   За 30 дней: {_format_num(analytics['messages_30d'])}\n"
        f"   Среднее/юзер: {analytics['avg_messages_per_user']}\n"
        f"⏰ Пиковый час: {peak_str}"
    )
    analytics_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📥 Экспорт подписчиков",
            callback_data=f"bot:export_subs:{bot_id}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"bot:manage:{bot_id}")],
    ])
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=analytics_kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bot:export_subs:"))
async def cb_bot_export_subs(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:export_subs:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    rows = await get_subscribers_for_export(bot_id, client_id)
    if rows is None:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    if not rows:
        await callback.answer("Подписчиков пока нет.", show_alert=True)
        return
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    bot_name = bot_cfg.bot_name if bot_cfg else str(bot_id)
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=["telegram_id", "joined_at"])
    writer.writeheader()
    for row in rows:
        joined = row["joined_at"]
        writer.writerow({
            "telegram_id": row["telegram_id"],
            "joined_at": joined.strftime("%Y-%m-%d %H:%M:%S") if joined else "",
        })
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"subscribers_{bot_name}_{date_str}.csv"
    if callback.message is not None:
        await callback.message.answer_document(
            BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=filename),
            caption=f"📥 {len(rows)} подписчиков",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("bot:conversations:"))
async def cb_bot_conversations(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:conversations:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    msgs = await get_bot_recent_conversations(bot_id, client_id, limit=20)
    if msgs is None:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    if not msgs:
        await callback.answer("Диалогов пока нет.", show_alert=True)
        return
    lines = ["💬 Последние диалоги (новые сверху)\n"]
    for m in msgs:
        ts = m["created_at"].strftime("%d.%m %H:%M") if m["created_at"] else ""
        who = f"@{m['username']}" if m["username"] else f"ID {m['telegram_id']}"
        icon = "👤" if m["role"] == "user" else "🤖"
        text_preview = (m["text"] or "")[:120].replace("\n", " ")
        if len(m["text"] or "") > 120:
            text_preview += "…"
        lines.append(f"{icon} {who} [{ts}]\n{text_preview}\n")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"bot:manage:{bot_id}")
    ]])
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=back_kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("bot:broadcast:"))
async def cb_bot_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "bot:broadcast:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    sub_count = await count_subscribers(bot_id)
    if sub_count == 0:
        if callback.message is not None:
            await callback.message.answer(
                "📢 У этого бота пока нет подписчиков.\n"
                "Подписчики добавляются автоматически когда пользователи пишут боту."
            )
        await callback.answer()
        return
    await state.set_state(BroadcastStates.confirm)
    await state.update_data(broadcast_bot_id=bot_id, broadcast_bot_token=bot_cfg.bot_token)
    if callback.message is not None:
        await callback.message.answer(
            f"📢 Рассылка для бота «{bot_cfg.bot_name}»\n"
            f"Подписчиков: {_format_num(sub_count)}\n\n"
            "Пришлите текст сообщения для рассылки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")
            ]]),
        )
    await callback.answer()


@router.callback_query(F.data == "broadcast:cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Рассылка отменена")
    if callback.message is not None:
        await callback.message.answer("Рассылка отменена. /mybots")


@router.message(BroadcastStates.confirm)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Пришлите сообщение для рассылки.")
        return
    data = await state.get_data()
    bot_id = data.get("broadcast_bot_id")
    bot_token = data.get("broadcast_bot_token")
    if not bot_id or not bot_token:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return

    subscriber_ids = await get_subscriber_ids(bot_id)
    if not subscriber_ids:
        await state.clear()
        await message.answer("Подписчиков не найдено. Рассылка отменена.")
        return

    await state.set_state(BroadcastStates.sending)
    await message.answer(f"⏳ Отправляю {_format_num(len(subscriber_ids))} подписчикам…")

    broadcast_bot = Bot(token=bot_token)
    sent = 0
    failed = 0
    # Telegram rate limit: 30 messages/sec; use ~25 to stay safe
    BATCH = 25
    try:
        for i, tg_id in enumerate(subscriber_ids):
            try:
                await broadcast_bot.send_message(tg_id, text)
                sent += 1
            except Exception:
                failed += 1
            if (i + 1) % BATCH == 0:
                await asyncio.sleep(1)
    finally:
        await broadcast_bot.session.close()

    await state.clear()
    await message.answer(
        f"✅ Рассылка завершена.\n"
        f"• Доставлено: {_format_num(sent)}\n"
        f"• Ошибок (бот заблокирован/не найден): {_format_num(failed)}"
    )


def _schedule_list_keyboard(bot_id: int, broadcasts: list) -> InlineKeyboardMarkup:
    rows = []
    for b in broadcasts:
        send_msk = b.send_at.astimezone(_MOSCOW_TZ)
        label = f"🕐 {send_msk.strftime('%d.%m %H:%M')} — {b.message_text[:30]}{'…' if len(b.message_text) > 30 else ''}"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"sched:noop:{b.id}"),
            InlineKeyboardButton(text="❌", callback_data=f"sched:cancel:{b.id}:{bot_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="➕ Запланировать рассылку", callback_data=f"sched:new:{bot_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"bot:detail:{bot_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("bot:schedule:"))
async def cb_bot_schedule(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    resolved = await _resolve_edit_target(callback, "bot:schedule:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    broadcasts = await get_bot_scheduled_broadcasts(bot_id, client_id)
    text = f"📅 Отложенные рассылки для «{bot_cfg.bot_name}»\n"
    if broadcasts:
        text += f"Запланировано: {len(broadcasts)}"
    else:
        text += "Нет запланированных рассылок."
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=_schedule_list_keyboard(bot_id, broadcasts))
    await callback.answer()


@router.callback_query(F.data.startswith("sched:cancel:"))
async def cb_schedule_cancel_item(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    client = await get_or_create_client(user.id, user.username)
    parts = (callback.data or "").split(":")
    try:
        broadcast_id = int(parts[2])
        bot_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    deleted = await cancel_scheduled_broadcast(broadcast_id, client.id)
    if not deleted:
        await callback.answer("Рассылка не найдена или уже отправлена", show_alert=True)
        return
    await callback.answer("Рассылка отменена")
    broadcasts = await get_bot_scheduled_broadcasts(bot_id, client.id)
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    bot_name = bot_cfg.bot_name if bot_cfg else "бот"
    text = f"📅 Отложенные рассылки для «{bot_name}»\n"
    text += f"Запланировано: {len(broadcasts)}" if broadcasts else "Нет запланированных рассылок."
    if callback.message is not None:
        await callback.message.edit_text(text, reply_markup=_schedule_list_keyboard(bot_id, broadcasts))


@router.callback_query(F.data.startswith("sched:noop:"))
async def cb_schedule_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("sched:new:"))
async def cb_schedule_new(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return
    sub_count = await count_subscribers(bot_id)
    if sub_count == 0:
        await callback.answer("У бота нет подписчиков", show_alert=True)
        return
    await state.set_state(ScheduledBroadcastStates.waiting_text)
    await state.update_data(sched_bot_id=bot_id, sched_client_id=client.id)
    if callback.message is not None:
        await callback.message.answer(
            f"📝 Введите текст рассылки для бота «{bot_cfg.bot_name}» ({_format_num(sub_count)} подписчиков):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="sched:cancel_flow"),
            ]]),
        )
    await callback.answer()


@router.callback_query(F.data == "sched:cancel_flow")
async def cb_schedule_cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message is not None:
        await callback.message.answer("Создание рассылки отменено. /mybots")


@router.message(ScheduledBroadcastStates.waiting_text)
async def on_schedule_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Введите сообщение для рассылки.")
        return
    await state.update_data(sched_text=text)
    await state.set_state(ScheduledBroadcastStates.waiting_time)
    await message.answer(
        "🕐 Когда отправить? Введите дату и время по Москве (UTC+3):\n\n"
        "Форматы:\n"
        "• <code>25.06 18:30</code> — 25 июня, 18:30 Мск\n"
        "• <code>25.06.2026 18:30</code> — полная дата",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="sched:cancel_flow"),
        ]]),
    )


@router.message(ScheduledBroadcastStates.waiting_time)
async def on_schedule_time(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    send_at_utc = _parse_schedule_time(raw)
    if send_at_utc is None:
        await message.answer(
            "Не удалось распознать время. Используйте формат <code>ДД.ММ ЧЧ:ММ</code> или <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.",
            parse_mode="HTML",
        )
        return
    if send_at_utc <= datetime.now(timezone.utc):
        await message.answer("Это время уже прошло. Укажите время в будущем.")
        return

    data = await state.get_data()
    bot_id = data.get("sched_bot_id")
    client_id = data.get("sched_client_id")
    sched_text = data.get("sched_text", "")
    if not bot_id or not client_id:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return

    await create_scheduled_broadcast(bot_id, client_id, sched_text, send_at_utc)
    await state.clear()

    send_msk = send_at_utc.astimezone(_MOSCOW_TZ)
    await message.answer(
        f"✅ Рассылка запланирована на <b>{send_msk.strftime('%d.%m.%Y %H:%M')} Мск</b>.\n"
        f"Управление: 📅 Расписание на карточке бота.",
        parse_mode="HTML",
    )


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
    # deploy_bot/deploy_vk_bot are idempotent — fast-start an existing stopped
    # container or rebuild from scratch if it was removed.
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    _platform = getattr(bot_cfg, "platform", "telegram") if bot_cfg else "telegram"
    try:
        if _platform == "vk":
            await deploy_vk_bot(bot_id)
        else:
            await deploy_bot(bot_id)
    except Exception:
        logger.exception("mybots: deploy failed bot_id={} platform={}", bot_id, _platform)
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


def _edit_menu_keyboard(bot_id: int, platform: str = "telegram") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📝 Системный промпт", callback_data=f"bot:edit_prompt:{bot_id}")],
        [InlineKeyboardButton(text="🎨 Имя и стиль", callback_data=f"bot:edit_style:{bot_id}")],
        [InlineKeyboardButton(text="🚫 Запреты", callback_data=f"bot:edit_forbidden:{bot_id}")],
        [InlineKeyboardButton(text="📜 Скрипты", callback_data=f"bot:edit_scripts:{bot_id}")],
        [InlineKeyboardButton(text="💬 Приветствие", callback_data=f"bot:edit_greeting:{bot_id}")],
        [InlineKeyboardButton(text="🚫 Чёрный список", callback_data=f"bot:blacklist:{bot_id}")],
        [InlineKeyboardButton(text="🔗 Webhook", callback_data=f"bot:edit_webhook:{bot_id}")],
        [InlineKeyboardButton(text="⚡ Триггеры", callback_data=f"bot:triggers:{bot_id}")],
        [InlineKeyboardButton(text="🛡 Лимит сообщений", callback_data=f"bot:rate_limit:{bot_id}")],
    ]
    if platform != "vk":
        rows.append([InlineKeyboardButton(
            text="📋 Кнопки быстрых ответов",
            callback_data=f"bot:quick_replies:{bot_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="🖼 Сгенерировать аватарку",
            callback_data=f"bot:set_avatar:{bot_id}",
        )])
    rows += [
        [InlineKeyboardButton(text="🏷 Переименовать", callback_data=f"bot:edit_name:{bot_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"bot:manage:{bot_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        platform = getattr(bot_cfg, "platform", "telegram")
        await callback.message.answer(
            f"🤖 {bot_cfg.bot_name} — что редактируем?",
            reply_markup=_edit_menu_keyboard(bot_id, platform=platform),
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


# --- Wave 25: bot avatar via FusionBrain ---

_AVATAR_PROMPT_BY_TYPE = {
    "support":        "customer support AI assistant, headset, friendly robot, minimalist flat icon",
    "seller":         "sales AI bot, shopping bag, ecommerce robot, minimalist flat icon",
    "content":        "content creator AI bot, pen and star, creative robot, minimalist flat icon",
    "parser":         "data parser AI bot, magnifying glass, analytics robot, minimalist flat icon",
    "service_orders": "service orders AI bot, wrench and gear, handyman robot, minimalist flat icon",
    "coach":          "life coach AI bot, lightbulb and person, motivational robot, minimalist flat icon",
    "creative":       "creative AI bot, paint palette, artistic robot, minimalist flat icon",
    "planner":        "planner AI bot, calendar and checkmark, organizer robot, minimalist flat icon",
    "edu":            "education AI bot, graduation cap and book, teacher robot, minimalist flat icon",
    "hr":             "HR AI bot, people and briefcase, recruiter robot, minimalist flat icon",
    "quiz":           "quiz AI bot, question mark and trophy, game show robot, minimalist flat icon",
    "real_estate":    "real estate AI bot, house and key, property robot, minimalist flat icon",
    "events":         "events AI bot, calendar and confetti, organizer robot, minimalist flat icon",
    "finance":        "finance AI bot, coins and chart, money robot, minimalist flat icon",
}


def _avatar_prompt(bot) -> str:
    base = _AVATAR_PROMPT_BY_TYPE.get(
        bot.bot_type,
        "AI chatbot robot, minimalist flat icon",
    )
    return f"{base}, blue and white color scheme, square format, professional"


@router.callback_query(F.data.startswith("bot:set_avatar:"))
async def cb_bot_set_avatar(callback: CallbackQuery) -> None:
    resolved = await _resolve_edit_target(callback, "bot:set_avatar:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return

    await callback.answer()
    if callback.message is None:
        return

    if not image_generator.enabled:
        await callback.message.answer(
            "⚠️ Генерация изображений отключена (FusionBrain ключи не заданы)."
        )
        return

    await callback.message.answer("⏳ Генерирую аватарку через FusionBrain (~30 сек)...")

    prompt = _avatar_prompt(bot_cfg)
    img_bytes = await image_generator.generate(prompt, width=512, height=512)

    if img_bytes is None:
        await callback.message.answer(
            "⚠️ Не удалось сгенерировать изображение. Попробуйте позже."
        )
        return

    from aiogram import Bot as _TempBot
    set_ok = False
    try:
        async with _TempBot(token=bot_cfg.bot_token) as temp_bot:
            await temp_bot.set_my_photo(
                photo=BufferedInputFile(img_bytes, filename="avatar.png")
            )
        set_ok = True
    except Exception:
        logger.exception("avatar: set_my_photo failed for bot_id={}", bot_id)

    if set_ok:
        await callback.message.answer_photo(
            BufferedInputFile(img_bytes, filename="avatar.png"),
            caption="✅ Аватарка установлена! Может появиться в Telegram с небольшой задержкой.",
        )
    else:
        await callback.message.answer_photo(
            BufferedInputFile(img_bytes, filename="avatar.png"),
            caption=(
                "⚠️ Изображение сгенерировано, но установить не удалось.\n"
                "Возможно токен бота недействителен или нет прав.\n"
                "Вы можете сохранить фото вручную."
            ),
        )


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
    client_id: int,
    telegram_id: int,
    *,
    is_combo: bool | None = None,
) -> tuple[bool, str, int, int]:
    """Returns (allowed, plan_name, current_count, limit).

    is_combo=None  — general pre-intake check: True if any slot (simple
                     or combo) is still available. count/limit are totals.
    is_combo=False — specific check for a single-type bot.
    is_combo=True  — specific check for a multi-type (combo) bot.

    Admins bypass the check entirely. Clients without an active
    subscription fall back to starter limits."""
    if is_admin(telegram_id):
        return True, "Безлимит (админ)", 0, 0
    sub = await get_active_subscription(client_id)
    tier = sub.tier if sub else "starter"
    plan = PLANS[tier]

    if is_combo is None:
        simple_limit = plan["simple_bots_limit"]
        combo_limit = plan["combo_bots_limit"]
        simple_count = await count_simple_bots(client_id)
        combo_count = await count_combo_bots(client_id)
        allowed = simple_count < simple_limit or combo_count < combo_limit
        return allowed, plan["name"], simple_count + combo_count, simple_limit + combo_limit

    if is_combo:
        limit = plan["combo_bots_limit"]
        count = await count_combo_bots(client_id)
    else:
        limit = plan["simple_bots_limit"]
        count = await count_simple_bots(client_id)
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
    # Write greeting.txt and rebuild container so the bot picks it up
    try:
        await asyncio.to_thread(write_bot_greeting, bot_id, text)
        await redeploy_bot(bot_id)
        await message.answer("👋 Приветствие сохранено и бот перезапущен.")
    except Exception:
        logger.exception("on_edit_greeting: redeploy failed bot_id={}", bot_id)
        await message.answer(
            "👋 Приветствие сохранено в настройках.\n"
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("bot:blacklist:"))
async def cb_bot_blacklist(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "bot:blacklist:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bl = await get_blacklist(bot_id, client_id)
    if bl is None:
        await callback.answer("Бот не найден.")
        return
    lines = [f"🚫 Чёрный список бота (ID: {bot_id})"]
    buttons: list[list[InlineKeyboardButton]] = []
    if bl:
        lines.append("")
        for tid in bl:
            lines.append(f"• {tid}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"❌ Удалить {tid}",
                    callback_data=f"blacklist:remove:{bot_id}:{tid}",
                )
            ])
    else:
        lines.append("\nСписок пуст.")
    buttons.append([
        InlineKeyboardButton(
            text="➕ Добавить ID",
            callback_data=f"blacklist:add:{bot_id}",
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"bot:edit:{bot_id}",
        )
    ])
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("blacklist:remove:"))
async def cb_blacklist_remove(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    # format: blacklist:remove:{bot_id}:{telegram_id}
    if len(parts) < 4:
        await callback.answer("Некорректные данные.")
        return
    try:
        bot_id = int(parts[2])
        telegram_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректные данные.")
        return
    client = await get_or_create_client(user.id, user.username)
    removed = await remove_from_blacklist(bot_id, client.id, telegram_id)
    if not removed:
        await callback.answer("Не найдено.")
        return
    # Sync file + redeploy
    bl = await get_blacklist(bot_id, client.id)
    try:
        await asyncio.to_thread(write_bot_blacklist, bot_id, bl or [])
        await redeploy_bot(bot_id)
        await callback.answer(f"✅ {telegram_id} удалён, бот перезапущен.")
    except Exception:
        logger.exception("cb_blacklist_remove: redeploy failed bot_id={}", bot_id)
        await callback.answer("Удалён из настроек. Перезапуск не удался.")
    # Refresh the blacklist view
    if callback.message is not None:
        await callback.message.delete()


@router.callback_query(F.data.startswith("blacklist:add:"))
async def cb_blacklist_add(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "blacklist:add:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите Telegram ID пользователя (число), которого хотите заблокировать."
        )
    await state.set_state(BlacklistStates.waiting_id)
    await state.update_data(blacklist_bot_id=bot_id)
    await callback.answer()


@router.message(BlacklistStates.waiting_id)
async def on_blacklist_id(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("Нужно число — Telegram ID. Попробуйте ещё раз.")
        return
    telegram_id = int(text)
    data = await state.get_data()
    bot_id = data.get("blacklist_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    added = await add_to_blacklist(bot_id, client.id, telegram_id)
    await state.clear()
    if not added:
        await message.answer(f"ID {telegram_id} уже в чёрном списке или бот не найден.")
        return
    bl = await get_blacklist(bot_id, client.id)
    try:
        await asyncio.to_thread(write_bot_blacklist, bot_id, bl or [])
        await redeploy_bot(bot_id)
        await message.answer(f"🚫 {telegram_id} добавлен в чёрный список и бот перезапущен.")
    except Exception:
        logger.exception("on_blacklist_id: redeploy failed bot_id={}", bot_id)
        await message.answer(
            f"🚫 {telegram_id} добавлен в настройки.\n"
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("bot:edit_webhook:"))
async def cb_bot_edit_webhook(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_webhook:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    current_url = (bot_cfg.config_json or {}).get("webhook_url", "") if bot_cfg else ""
    hint = f"\nТекущий URL: {current_url}" if current_url else "\nWebhook не настроен."
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите URL для webhook (https://...) — бот будет POST'ить каждое сообщение туда.\n"
            "Чтобы отключить — пришлите «-»." + hint
        )
    await state.set_state(EditStates.waiting_webhook)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_webhook)
async def on_edit_webhook(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите URL или «-» чтобы отключить.")
        return
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    url = "" if text == "-" else text
    if url and not url.startswith(("http://", "https://")):
        await message.answer("URL должен начинаться с http:// или https://. Попробуйте ещё раз.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(bot_id, client.id, "webhook_url", url)
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    await state.clear()
    try:
        await asyncio.to_thread(write_bot_webhook_url, bot_id, url)
        await redeploy_bot(bot_id)
        if url:
            await message.answer(f"🔗 Webhook сохранён и бот перезапущен.\nURL: {url}")
        else:
            await message.answer("🔗 Webhook отключён и бот перезапущен.")
    except Exception:
        logger.exception("on_edit_webhook: redeploy failed bot_id={}", bot_id)
        await message.answer(
            "🔗 Webhook сохранён в настройках.\n"
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("bot:triggers:"))
async def cb_bot_triggers(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "bot:triggers:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    triggers = await get_triggers(bot_id, client_id)
    if triggers is None:
        await callback.answer("Бот не найден.")
        return
    lines = [f"⚡ Триггеры бота (ID: {bot_id})"]
    buttons: list[list[InlineKeyboardButton]] = []
    if triggers:
        lines.append("")
        for kw, resp in triggers.items():
            preview = resp[:40] + "…" if len(resp) > 40 else resp
            lines.append(f"• {kw} → {preview}")
            buttons.append([InlineKeyboardButton(
                text=f"❌ Удалить «{kw}»",
                callback_data=f"trigger:remove:{bot_id}:{kw}",
            )])
    else:
        lines.append("\nТриггеров нет.")
    buttons.append([InlineKeyboardButton(
        text="➕ Добавить триггер",
        callback_data=f"trigger:add:{bot_id}",
    )])
    buttons.append([InlineKeyboardButton(
        text="◀️ Назад",
        callback_data=f"bot:edit:{bot_id}",
    )])
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("trigger:add:"))
async def cb_trigger_add(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "trigger:add:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите ключевое слово (одно слово или фраза).\n"
            "Пример: цена"
        )
    await state.set_state(TriggerStates.waiting_keyword)
    await state.update_data(trigger_bot_id=bot_id)
    await callback.answer()


@router.message(TriggerStates.waiting_keyword)
async def on_trigger_keyword(message: Message, state: FSMContext) -> None:
    keyword = (message.text or "").strip()
    if not keyword:
        await message.answer("Пустое слово. Пришлите ключевое слово.")
        return
    if len(keyword) > 100:
        await message.answer("Слово слишком длинное (макс. 100 символов).")
        return
    await state.update_data(trigger_keyword=keyword)
    await state.set_state(TriggerStates.waiting_response)
    await message.answer(f"Ключевое слово: «{keyword}»\nТеперь пришлите ответ бота на это слово.")


@router.message(TriggerStates.waiting_response)
async def on_trigger_response(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    response = (message.text or "").strip()
    if not response:
        await message.answer("Пустой ответ. Пришлите текст ответа.")
        return
    data = await state.get_data()
    bot_id = data.get("trigger_bot_id")
    keyword = data.get("trigger_keyword")
    if bot_id is None or keyword is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await set_trigger(bot_id, client.id, keyword, response)
    await state.clear()
    if not ok:
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    triggers = await get_triggers(bot_id, client.id)
    try:
        await asyncio.to_thread(write_bot_triggers, bot_id, triggers or {})
        await redeploy_bot(bot_id)
        await message.answer(f"⚡ Триггер «{keyword}» сохранён и бот перезапущен.")
    except Exception:
        logger.exception("on_trigger_response: redeploy failed bot_id={}", bot_id)
        await message.answer(
            f"⚡ Триггер «{keyword}» сохранён в настройках.\n"
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("trigger:remove:"))
async def cb_trigger_remove(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    # format: trigger:remove:{bot_id}:{keyword}
    raw = (callback.data or "").removeprefix("trigger:remove:")
    colon = raw.index(":")
    try:
        bot_id = int(raw[:colon])
    except ValueError:
        await callback.answer("Некорректные данные.")
        return
    keyword = raw[colon + 1:]
    client = await get_or_create_client(user.id, user.username)
    removed = await remove_trigger(bot_id, client.id, keyword)
    if not removed:
        await callback.answer("Триггер не найден.")
        return
    triggers = await get_triggers(bot_id, client.id)
    try:
        await asyncio.to_thread(write_bot_triggers, bot_id, triggers or {})
        await redeploy_bot(bot_id)
        await callback.answer(f"✅ «{keyword}» удалён, бот перезапущен.")
    except Exception:
        logger.exception("cb_trigger_remove: redeploy failed bot_id={}", bot_id)
        await callback.answer("Удалён из настроек. Перезапуск не удался.")
    if callback.message is not None:
        await callback.message.delete()


@router.callback_query(F.data.startswith("bot:rate_limit:"))
async def cb_bot_rate_limit(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve_edit_target(callback, "bot:rate_limit:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    bot_cfg = await get_bot_by_id(bot_id, client_id)
    current = (bot_cfg.config_json or {}).get("rate_limit_per_hour", 0) if bot_cfg else 0
    hint = f"Текущий лимит: {current} сообщений/час." if current else "Лимит не задан (нет ограничений)."
    if callback.message is not None:
        await callback.message.answer(
            f"🛡 Лимит сообщений на пользователя\n{hint}\n\n"
            "Введите максимальное количество сообщений в час (число от 1 до 1000).\n"
            "Чтобы отключить лимит — введите 0."
        )
    await state.set_state(RateLimitStates.waiting_limit)
    await state.update_data(rate_limit_bot_id=bot_id)
    await callback.answer()


@router.message(RateLimitStates.waiting_limit)
async def on_rate_limit(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужно целое число (например: 30). Попробуйте ещё раз.")
        return
    value = int(text)
    if value > 1000:
        await message.answer("Максимум 1000 сообщений/час. Введите число от 0 до 1000.")
        return
    data = await state.get_data()
    bot_id = data.get("rate_limit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await update_bot_config(bot_id, client.id, "rate_limit_per_hour", value)
    await state.clear()
    if not ok:
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    try:
        await asyncio.to_thread(write_bot_rate_limit, bot_id, value)
        await redeploy_bot(bot_id)
        if value:
            await message.answer(f"🛡 Лимит {value} сообщений/час сохранён и бот перезапущен.")
        else:
            await message.answer("🛡 Лимит отключён и бот перезапущен.")
    except Exception:
        logger.exception("on_rate_limit: redeploy failed bot_id={}", bot_id)
        await message.answer(
            "🛡 Лимит сохранён в настройках.\n"
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("bot:quick_replies:"))
async def cb_bot_quick_replies(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:quick_replies:")
    if resolved is None:
        return
    bot_id, client_id = resolved
    buttons = await get_quick_replies(bot_id, client_id)
    if buttons is None:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    if not buttons:
        text = "📋 Кнопки быстрых ответов не заданы."
    else:
        listed = "\n".join(f"{i+1}. {b}" for i, b in enumerate(buttons))
        text = f"📋 Кнопки быстрых ответов:\n{listed}"
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {b}",
            callback_data=f"bot:qr_remove:{bot_id}:{i}",
        )]
        for i, b in enumerate(buttons)
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Добавить кнопку",
        callback_data=f"bot:qr_add:{bot_id}",
    )])
    rows.append([InlineKeyboardButton(
        text="◀️ Назад",
        callback_data=f"bot:edit_menu:{bot_id}",
    )])
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("bot:qr_add:"))
async def cb_quick_reply_add(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:qr_add:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Введите текст кнопки (не более 64 символов).\n/cancel чтобы отменить."
        )
    await state.set_state(QuickReplyStates.waiting_button)
    await state.update_data(qr_bot_id=bot_id)
    await callback.answer()


@router.message(QuickReplyStates.waiting_button)
async def on_quick_reply_button(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if len(text) > 64:
        await message.answer("Слишком длинный текст. Максимум 64 символа. Попробуйте ещё раз.")
        return
    if not text:
        await message.answer("Текст не может быть пустым. Введите текст кнопки.")
        return
    data = await state.get_data()
    bot_id = data.get("qr_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    buttons = await get_quick_replies(bot_id, client.id) or []
    if len(buttons) >= 10:
        await state.clear()
        await message.answer("Максимум 10 кнопок. Удалите ненужные через меню.")
        return
    buttons.append(text)
    ok = await set_quick_replies(bot_id, client.id, buttons)
    await state.clear()
    if not ok:
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    try:
        await asyncio.to_thread(write_bot_quick_replies, bot_id, buttons)
        await redeploy_bot(bot_id)
        await message.answer(f'📋 Кнопка «{text}» добавлена и бот перезапущен.')
    except Exception:
        logger.exception("on_quick_reply_button: redeploy failed bot_id={}", bot_id)
        await message.answer(
            f'📋 Кнопка «{text}» сохранена в настройках.\n'
            "⚠️ Не удалось перезапустить контейнер — изменение вступит в силу при следующем деплое."
        )


@router.callback_query(F.data.startswith("bot:qr_remove:"))
async def cb_quick_reply_remove(
    callback: CallbackQuery, state: FSMContext
) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Неверные данные.", show_alert=True)
        return
    try:
        bot_id = int(parts[2])
        idx = int(parts[3])
    except ValueError:
        await callback.answer("Неверные данные.", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    buttons = await get_quick_replies(bot_id, client.id)
    if buttons is None:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    if idx < 0 or idx >= len(buttons):
        await callback.answer("Кнопка не найдена.", show_alert=True)
        return
    removed = buttons.pop(idx)
    ok = await set_quick_replies(bot_id, client.id, buttons)
    if not ok:
        await callback.answer("Ошибка сохранения.", show_alert=True)
        return
    try:
        await asyncio.to_thread(write_bot_quick_replies, bot_id, buttons)
        await redeploy_bot(bot_id)
        await callback.answer(f'Кнопка «{removed}» удалена.')
    except Exception:
        logger.exception("cb_quick_reply_remove: redeploy failed bot_id={}", bot_id)
        await callback.answer(f'Кнопка «{removed}» удалена (перезапуск при следующем деплое).')
    if callback.message is not None:
        if not buttons:
            text = "📋 Кнопки быстрых ответов не заданы."
        else:
            listed = "\n".join(f"{i+1}. {b}" for i, b in enumerate(buttons))
            text = f"📋 Кнопки быстрых ответов:\n{listed}"
        rows = [
            [InlineKeyboardButton(
                text=f"❌ {b}",
                callback_data=f"bot:qr_remove:{bot_id}:{i}",
            )]
            for i, b in enumerate(buttons)
        ]
        rows.append([InlineKeyboardButton(
            text="➕ Добавить кнопку",
            callback_data=f"bot:qr_add:{bot_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"bot:edit_menu:{bot_id}",
        )])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("bot:edit_name:"))
async def cb_bot_edit_name(
    callback: CallbackQuery, state: FSMContext
) -> None:
    resolved = await _resolve_edit_target(callback, "bot:edit_name:")
    if resolved is None:
        return
    bot_id, _ = resolved
    if callback.message is not None:
        await callback.message.answer(
            "Пришлите новое имя бота одним сообщением (не более 64 символов)."
        )
    await state.set_state(EditStates.waiting_rename)
    await state.update_data(edit_bot_id=bot_id)
    await callback.answer()


@router.message(EditStates.waiting_rename)
async def on_edit_rename(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое имя. Пришлите название бота.")
        return
    if len(text) > 64:
        await message.answer("Имя слишком длинное (макс. 64 символа). Попробуйте ещё раз.")
        return
    data = await state.get_data()
    bot_id = data.get("edit_bot_id")
    if bot_id is None:
        await state.clear()
        await message.answer("Сессия потеряна. /mybots чтобы начать заново.")
        return
    client = await get_or_create_client(user.id, user.username)
    ok = await rename_bot(bot_id, client.id, text)
    if not ok:
        await state.clear()
        await message.answer("Бот не найден. /mybots чтобы выбрать другой.")
        return
    await state.clear()
    await message.answer(f"🏷 Бот переименован в «{text}». /mybots")


@router.callback_query(F.data.startswith("bot:clone:"))
async def cb_bot_clone(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or callback.message is None:
        await callback.answer()
        return
    try:
        bot_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return
    client = await get_or_create_client(user.id, user.username)
    bot_cfg = await get_bot_by_id(bot_id, client.id)
    if bot_cfg is None:
        await callback.answer("Бот не найден", show_alert=True)
        return

    _is_combo = bool((bot_cfg.config_json or {}).get("merged_types"))
    allowed, plan_name, count, limit = await _check_bots_limit(
        client.id, user.id, is_combo=_is_combo
    )
    if not allowed:
        _kind = "комбо-ботов" if _is_combo else "простых ботов"
        await callback.answer(
            f"Лимит {_kind} на тарифе {plan_name} ({count}/{limit}).\n"
            "Удалите бота или обновите тариф → /subscribe",
            show_alert=True,
        )
        return

    await state.set_state(CloneStates.waiting_token)
    await state.update_data(clone_source_bot_id=bot_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="clone:cancel")
        ]]
    )
    await callback.message.answer(
        f"🔁 Клонирование бота <b>{bot_cfg.bot_name}</b>\n\n"
        "Клон получит те же настройки, промпт и тип.\n"
        "Нужен отдельный токен от @BotFather — один токен нельзя использовать дважды.\n\n"
        "Отправьте токен нового бота:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "clone:cancel")
async def cb_clone_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message is not None:
        await callback.message.answer("Клонирование отменено.")
    await callback.answer()


@router.message(CloneStates.waiting_token)
async def on_clone_token(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    new_token = (message.text or "").strip()
    try:
        validate_token(new_token)
    except TokenValidationError:
        await message.answer(
            "Это не похоже на токен бота. Проверьте формат и отправьте ещё раз."
        )
        return

    data = await state.get_data()
    source_bot_id = data.get("clone_source_bot_id")
    if source_bot_id is None:
        await message.answer("Сессия устарела. Начните клонирование заново.")
        await state.clear()
        return

    client = await get_or_create_client(user.id, user.username)
    await message.answer("Клонирую бота, подождите...")
    try:
        clone = await clone_bot_config(source_bot_id, client.id, new_token)
    except ValueError:
        await message.answer("Исходный бот не найден. Попробуйте ещё раз.")
        await state.clear()
        return

    try:
        await asyncio.to_thread(clone_bot_files, source_bot_id, clone.id)
    except FileNotFoundError:
        await message.answer(
            "⚠️ Файлы исходного бота не найдены на диске.\n"
            "Возможно, бот был создан давно. Попробуйте пересоздать его."
        )
        await state.clear()
        return

    deploy_ok = False
    try:
        await deploy_bot(clone.id)
        deploy_ok = True
    except Exception:
        logger.exception("clone: deploy_bot failed for clone_id={}", clone.id)

    await state.clear()
    if deploy_ok:
        await message.answer(
            f"✅ Клон создан и запущен!\n\n"
            f"🤖 {clone.bot_name}\n"
            "Управление → /mybots"
        )
    else:
        await message.answer(
            f"⚠️ Клон создан, но контейнер не поднялся.\n"
            f"🤖 {clone.bot_name}\n"
            "Проверьте токен и попробуйте перезапустить через /mybots"
        )
    logger.info(
        "clone: ok source={} clone={} client_id={}", source_bot_id, clone.id, client.id
    )


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
    if not await _require_consent(message):
        return

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
        if tier == "business":
            return "\n\n🔒 Лимит 50М токенов исчерпан. /help для кастомного тарифа"
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
    if not await _require_consent(message):
        return

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
    if not await _require_consent(message):
        return

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


@router.message(Command("revoke_consent"))
async def cmd_revoke_consent(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    try:
        await revoke_consent(user.id)
    except Exception:
        logger.exception("intake: revoke_consent failed for tg_id={}", user.id)
        await message.answer("Не удалось отозвать согласие. Попробуйте позже.")
        return

    logger.info("intake: consent revoked tg_id={}", user.id)
    await state.clear()
    await message.answer(
        "Согласие на обработку персональных данных отозвано.\n\n"
        "Ваши данные остаются в системе до явного удаления.\n"
        "Для полного удаления данных используйте /delete_my_data\n\n"
        "⚠️ Без согласия дальнейшее использование сервиса невозможно.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("my_data"))
async def cmd_my_data(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    try:
        summary = await get_client_summary(user.id)
    except Exception:
        logger.exception("my_data: failed for tg_id={}", user.id)
        await message.answer("Не удалось получить данные. Попробуйте позже.")
        return

    if summary is None:
        await message.answer("Вы не зарегистрированы. Используйте /start.")
        return

    lines = ["📋 Ваши данные в системе:", ""]
    lines.append(f"Telegram ID: {summary['telegram_id']}")
    if summary["username"]:
        lines.append(f"Username: @{summary['username']}")
    else:
        lines.append("Username: не сохранён")
    lines.append("")
    consent_label = "дано" if summary["consent_given"] else "не дано / отозвано"
    lines.append(f"Согласие на обработку: {consent_label}")
    if summary["consent_at"]:
        dt = summary["consent_at"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"Дата согласия: {dt}")
    if summary["created_at"]:
        lines.append(f"Дата регистрации: {summary['created_at'].strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"Ботов создано: {summary['bot_count']}")
    if summary["subscription_tier"]:
        lines.append(f"Подписка: тариф {summary['subscription_tier']}")
        if summary["subscription_expires_at"]:
            exp = summary["subscription_expires_at"].strftime("%Y-%m-%d")
            lines.append(f"Истекает: {exp}")
    else:
        lines.append("Подписка: нет активной")
    lines.append(f"Сообщений в истории чата: {summary['chat_message_count']}")
    lines.append(f"Фрагментов базы знаний: {summary['knowledge_chunk_count']}")
    lines.extend([
        "",
        "Для удаления данных: /delete_my_data",
        "Для отзыва согласия: /revoke_consent",
    ])

    await message.answer("\n".join(lines))


@router.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    client = await get_or_create_client(user.id, user.username)
    code = await get_or_create_referral_code(client.id)
    stats = await get_referral_stats(client.id)

    bot_username = _BOT_USERNAME or "ArmyBotsBot"
    link = f"https://t.me/{bot_username}?start=ref_{code}"

    lines = [
        "🤝 Реферальная программа",
        "",
        "Приглашайте друзей — получайте +30 дней Про за каждого, кто оформит подписку.",
        "",
        f"Ваша ссылка:\n{link}",
        "",
        f"Приглашено друзей: {stats['total_referrals']}",
        f"Наград получено: {stats['rewards_earned']}",
    ]
    if stats["pending_rewards"]:
        lines.append(f"Ожидает оплаты (друзья ещё не купили): {stats['pending_rewards']}")

    await message.answer("\n".join(lines))


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


@router.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message) -> None:
    user = message.from_user
    if not is_admin(user.id if user else None):
        return

    try:
        stats = await get_admin_stats()
    except Exception:
        logger.exception("admin_stats: query failed")
        await message.answer("Ошибка при получении статистики.")
        return

    tier_names = {"starter": "Старт", "pro": "Про", "business": "Бизнес"}
    tier_prices = {"starter": 490, "pro": 949, "business": 2990}

    sub_lines = []
    for tier in ("starter", "pro", "business"):
        cnt = stats["tier_counts"].get(tier, 0)
        if cnt:
            price = tier_prices[tier]
            sub_lines.append(f"  {tier_names[tier]}: {cnt} × {price}₽ = {cnt * price:,}₽".replace(",", " "))

    top_lines = []
    for i, u in enumerate(stats["top_users"], 1):
        name = f"@{u['username']}" if u["username"] else f"id:{u['telegram_id']}"
        top_lines.append(f"  {i}. {name} — {u['tokens_used']:,}".replace(",", " "))

    now_msk = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    text = (
        f"📊 ArmyBots Dashboard\n"
        f"🕐 {now_msk}\n\n"
        f"👥 Клиентов: {stats['client_count']}\n"
        f"🤖 Ботов в системе: {stats['bot_count']}\n\n"
        f"💰 Подписки: {stats['total_active_subs']} активных\n"
        + ("\n".join(sub_lines) + "\n" if sub_lines else "  (нет активных)\n")
        + f"MRR: ~{stats['mrr']:,}₽\n".replace(",", " ")
        + f"ARR: ~{stats['mrr'] * 12:,}₽\n\n".replace(",", " ")
        + f"📈 Токены (активные подписки): {stats['tokens_total']:,}\n".replace(",", " ")
        + f"💵 Расход всего: ${stats['cost_total_usd']:.2f}\n"
    )
    if top_lines:
        text += "\n🏆 Топ по токенам:\n" + "\n".join(top_lines)

    await message.answer(text)


BOT_TOKEN = settings.bot_token
_BOT_USERNAME: str | None = None

bot = Bot(token=BOT_TOKEN)
storage = RedisStorage.from_url(settings.redis_url)
dp = Dispatcher(storage=storage)
dp.message.middleware(ConsentGateMiddleware())
dp.callback_query.middleware(ConsentGateMiddleware())
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

    global _BOT_USERNAME
    bot_info = await bot.get_me()
    _BOT_USERNAME = bot_info.username
    logger.info("Бот запущен: @{}", _BOT_USERNAME)

    scheduler = start_alerts_scheduler(bot)
    attach_broadcasts_scheduler(scheduler, bot)
    attach_health_monitor(scheduler, bot)

    polling_task = asyncio.create_task(
        dp.start_polling(bot), name="polling"
    )
    webhook_task = asyncio.create_task(
        start_webhook_server(bot), name="webhook"
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
