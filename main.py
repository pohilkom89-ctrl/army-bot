import asyncio
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from loguru import logger

from db.database import get_session, init_db  # noqa: F401
from db.repository import (
    anonymize_user,
    get_or_create_client,
    save_consent,
)
from pipeline import run_pipeline


CONSENT_TEXT = """Для создания бота мы обрабатываем ваш Telegram ID и username.
Данные хранятся на серверах в России, третьим лицам не передаются.
Вы можете удалить свои данные командой /delete_my_data

Нажмите Согласен чтобы продолжить."""


class IntakeStates(StatesGroup):
    consent = State()
    ask_type = State()
    ask_purpose = State()
    ask_audience = State()
    processing = State()


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
        "Какой бот нужен? (парсер / контент / продажи / другое)",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(IntakeStates.consent, F.text == "Не согласен")
async def on_consent_no(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Без согласия продолжить невозможно. /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(IntakeStates.ask_type)
async def on_type(message: Message, state: FSMContext) -> None:
    await state.update_data(bot_type=(message.text or "").strip())
    await state.set_state(IntakeStates.ask_purpose)
    await message.answer("Опиши задачу бота в 2-3 предложениях.")


@router.message(IntakeStates.ask_purpose)
async def on_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=(message.text or "").strip())
    await state.set_state(IntakeStates.ask_audience)
    await message.answer("Кто будет им пользоваться?")


@router.message(IntakeStates.ask_audience)
async def on_audience(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return

    await state.update_data(audience=(message.text or "").strip())
    data = await state.get_data()
    answers = {
        "bot_type": data.get("bot_type"),
        "purpose": data.get("purpose"),
        "audience": data.get("audience"),
    }

    await state.set_state(IntakeStates.processing)
    await message.answer("Агенты приступили к работе, ожидайте ~60 секунд...")

    logger.info("intake: pipeline launched for tg_id={} answers={}", user.id, answers)
    try:
        spec = await asyncio.to_thread(run_pipeline, answers)
    except Exception as err:
        logger.exception("intake: pipeline failed for tg_id={}", user.id)
        await message.answer(f"Не удалось сгенерировать бота: {err}")
        await state.clear()
        return

    logger.info(
        "intake: pipeline ok for tg_id={} (code_len={} bytes)",
        user.id,
        len(spec.bot_code),
    )
    await message.answer(
        f"Бот готов! Код сохранён.\nРазмер кода: {len(spec.bot_code)} байт."
    )
    await state.clear()


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
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


async def main():
    await init_db()
    logger.info("База данных инициализирована")
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
