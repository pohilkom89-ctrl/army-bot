"""LMS (Learning Management System) bot runtime.

Reads course content from /app/course.json on each message (hot-reload
without container restart). Tracks student progress in /app/progress.json.

Course JSON schema:
{
  "title": "str",
  "welcome": "str",
  "completion_message": "str",
  "modules": [
    {
      "id": "m1",
      "title": "str",
      "lessons": [
        {
          "id": "m1_l1",
          "title": "str",
          "content": "str",
          "quiz": null | {
            "question": "str",
            "options": ["A", "B", "C"],
            "correct": 0,
            "explanation": "str"
          }
        }
      ]
    }
  ]
}
"""
import asyncio
import json as _json
import os
import time
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8").strip()

COURSE_FILE = Path("/app/course.json")
PROGRESS_FILE = Path("/app/progress.json")

openai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

_course_mtime: float = 0.0
_course_cache: dict = {}
_progress: dict[str, dict] = {}


def _load_course() -> dict:
    global _course_mtime, _course_cache
    try:
        mtime = COURSE_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _course_mtime:
        try:
            _course_cache = _json.loads(COURSE_FILE.read_text(encoding="utf-8"))
            _course_mtime = mtime
        except Exception:
            pass
    return _course_cache


def _load_progress() -> None:
    global _progress
    try:
        _progress = _json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _progress = {}


def _save_progress() -> None:
    try:
        PROGRESS_FILE.write_text(_json.dumps(_progress, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _get_student(uid: int) -> dict:
    key = str(uid)
    if key not in _progress:
        _progress[key] = {"completed": [], "current": None, "quiz_pending": None}
    return _progress[key]


def _all_lessons(course: dict) -> list[dict]:
    """Flat list of all lessons with module info added."""
    out = []
    for mod in course.get("modules", []):
        for lesson in mod.get("lessons", []):
            out.append({**lesson, "_module_title": mod["title"]})
    return out


def _lesson_by_id(course: dict, lesson_id: str) -> Optional[dict]:
    for lesson in _all_lessons(course):
        if lesson["id"] == lesson_id:
            return lesson
    return None


def _next_lesson_id(course: dict, current_id: Optional[str]) -> Optional[str]:
    lessons = _all_lessons(course)
    if not lessons:
        return None
    if current_id is None:
        return lessons[0]["id"]
    for i, l in enumerate(lessons):
        if l["id"] == current_id and i + 1 < len(lessons):
            return lessons[i + 1]["id"]
    return None


def _prev_lesson_id(course: dict, current_id: str) -> Optional[str]:
    lessons = _all_lessons(course)
    for i, l in enumerate(lessons):
        if l["id"] == current_id and i > 0:
            return lessons[i - 1]["id"]
    return None


def _lesson_keyboard(course: dict, lesson_id: str, can_advance: bool) -> InlineKeyboardMarkup:
    prev_id = _prev_lesson_id(course, lesson_id)
    next_id = _next_lesson_id(course, lesson_id)
    row = []
    if prev_id:
        row.append(InlineKeyboardButton(text="← Назад", callback_data=f"lms:go:{prev_id}"))
    if can_advance and next_id:
        row.append(InlineKeyboardButton(text="Далее →", callback_data=f"lms:go:{next_id}"))
    elif not can_advance:
        row.append(InlineKeyboardButton(text="✅ Ответьте на вопрос выше", callback_data="lms:noop"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton(text="📋 Меню курса", callback_data="lms:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_lesson(bot: Bot, chat_id: int, uid: int, lesson_id: str) -> None:
    course = _load_course()
    lesson = _lesson_by_id(course, lesson_id)
    if not lesson:
        await bot.send_message(chat_id, "Урок не найден.")
        return
    student = _get_student(uid)
    student["current"] = lesson_id
    student["quiz_pending"] = None

    quiz = lesson.get("quiz")
    can_advance = quiz is None or lesson_id in student["completed"]
    header = f"📖 {lesson['_module_title']} — {lesson['title']}\n\n"
    await bot.send_message(chat_id, header + lesson["content"],
                           reply_markup=_lesson_keyboard(course, lesson_id, can_advance))

    if quiz and lesson_id not in student["completed"]:
        student["quiz_pending"] = lesson_id
        opts_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{chr(65+i)}. {opt}",
                                  callback_data=f"lms:ans:{lesson_id}:{i}")]
            for i, opt in enumerate(quiz["options"])
        ])
        await bot.send_message(chat_id, f"❓ {quiz['question']}", reply_markup=opts_kb)

    _save_progress()


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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


async def _ensure_history_loaded(uid: int) -> None:
    if uid in _history_loaded:
        return
    _history_loaded.add(uid)
    try:
        msgs = await load_history(uid)
        if msgs:
            _history[uid] = msgs[-MAX_HISTORY:]
    except Exception:
        pass


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    _get_student(uid)  # init if needed
    asyncio.create_task(report_subscriber(uid))
    course = _load_course()
    if not course:
        await message.answer("Курс ещё не настроен. Скоро появится!")
        return
    welcome = course.get("welcome", "Добро пожаловать на курс!")
    first_id = _next_lesson_id(course, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать курс", callback_data=f"lms:go:{first_id}")],
        [InlineKeyboardButton(text="📋 Меню курса", callback_data="lms:menu")],
    ]) if first_id else None
    await message.answer(f"📚 {course.get('title', 'Курс')}\n\n{welcome}", reply_markup=kb)


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await _show_menu(message.from_user.id, message.chat.id)


@dp.message(Command("progress"))
async def cmd_progress(message: Message) -> None:
    uid = message.from_user.id
    course = _load_course()
    all_l = _all_lessons(course)
    if not all_l:
        await message.answer("Курс ещё не настроен.")
        return
    student = _get_student(uid)
    done = len(student.get("completed", []))
    total = len(all_l)
    pct = int(done / total * 100) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await message.answer(f"📊 Ваш прогресс\n\n[{bar}] {pct}%\n{done} из {total} уроков пройдено")


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    uid = message.from_user.id
    _progress[str(uid)] = {"completed": [], "current": None, "quiz_pending": None}
    _save_progress()
    await message.answer("Прогресс сброшен. /start чтобы начать заново.")


async def _show_menu(uid: int, chat_id: int) -> None:
    course = _load_course()
    if not course:
        await bot.send_message(chat_id, "Курс не настроен.")
        return
    student = _get_student(uid)
    completed = set(student.get("completed", []))
    lines = [f"📚 {course.get('title', 'Курс')}\n"]
    rows = []
    for mod in course.get("modules", []):
        lines.append(f"\n🗂 {mod['title']}")
        for lesson in mod.get("lessons", []):
            mark = "✅" if lesson["id"] in completed else ("▶️" if lesson["id"] == student.get("current") else "○")
            lines.append(f"  {mark} {lesson['title']}")
            rows.append([InlineKeyboardButton(
                text=f"{mark} {lesson['title'][:40]}",
                callback_data=f"lms:go:{lesson['id']}",
            )])
    rows.append([InlineKeyboardButton(text="📊 Мой прогресс", callback_data="lms:progress")])
    await bot.send_message(chat_id, "\n".join(lines),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("lms:go:"))
async def cb_go(callback: CallbackQuery) -> None:
    lesson_id = callback.data[len("lms:go:"):]
    uid = callback.from_user.id
    await _send_lesson(bot, callback.message.chat.id, uid, lesson_id)
    await callback.answer()


@dp.callback_query(F.data == "lms:menu")
async def cb_menu(callback: CallbackQuery) -> None:
    await _show_menu(callback.from_user.id, callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "lms:progress")
async def cb_progress_cb(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    course = _load_course()
    all_l = _all_lessons(course)
    student = _get_student(uid)
    done = len(student.get("completed", []))
    total = len(all_l)
    pct = int(done / total * 100) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await callback.answer(f"[{bar}] {pct}% — {done}/{total} уроков", show_alert=True)


@dp.callback_query(F.data == "lms:noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer("Сначала ответьте на вопрос!", show_alert=True)


@dp.callback_query(F.data.startswith("lms:ans:"))
async def cb_answer(callback: CallbackQuery) -> None:
    # lms:ans:{lesson_id}:{choice_index}
    parts = callback.data.split(":")
    lesson_id = parts[2]
    choice = int(parts[3])
    uid = callback.from_user.id
    course = _load_course()
    lesson = _lesson_by_id(course, lesson_id)
    if not lesson or not lesson.get("quiz"):
        await callback.answer()
        return
    quiz = lesson["quiz"]
    student = _get_student(uid)
    correct = quiz["correct"]
    if choice == correct:
        student["completed"].append(lesson_id)
        if lesson_id in (student.get("quiz_pending") or [lesson_id]):
            student["quiz_pending"] = None
        _save_progress()
        explanation = quiz.get("explanation", "")
        msg = f"✅ Верно!" + (f"\n\n{explanation}" if explanation else "")
        next_id = _next_lesson_id(course, lesson_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Далее →", callback_data=f"lms:go:{next_id}")]
            if next_id else
            [InlineKeyboardButton(text="🏁 Завершить курс", callback_data="lms:complete")],
            [InlineKeyboardButton(text="📋 Меню", callback_data="lms:menu")],
        ])
        await callback.message.answer(msg, reply_markup=kb)
    else:
        await callback.answer("❌ Неверно. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "lms:complete")
async def cb_complete(callback: CallbackQuery) -> None:
    course = _load_course()
    msg = course.get("completion_message", "🎓 Поздравляем! Вы прошли курс целиком.")
    await callback.message.answer(msg)
    await callback.answer()


@dp.message()
async def on_message(message: Message) -> None:
    """Fallback: answer off-topic questions via LLM using the instructor persona."""
    if not message.text:
        return
    uid = message.from_user.id
    text = message.text.strip()
    await _ensure_history_loaded(uid)
    asyncio.create_task(report_message(uid, str(uid), "user", text))
    _append_history(uid, "user", text)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + _get_history(uid)
    try:
        resp = await openai_client.chat.completions.create(
            model=MODEL, max_tokens=1024, messages=msgs
        )
        asyncio.create_task(report_usage(resp.usage, MODEL))
        reply = resp.choices[0].message.content or ""
        _append_history(uid, "assistant", reply)
        asyncio.create_task(report_message(uid, str(uid), "bot", reply))
        await message.answer(reply)
    except Exception:
        logger.exception("lms: LLM failed uid={}", uid)
        await message.answer("Произошла ошибка. Попробуйте позже.")


async def main() -> None:
    _load_progress()
    logger.info("LMS bot starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
