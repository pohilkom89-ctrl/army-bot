"""Owner-only debug bot for ArmyBots.

Analyzes Python tracebacks, reads relevant file sections, proposes a code fix,
and applies it after owner confirmation (git commit included).

Run with: python debug_bot.py
"""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from openai import AsyncOpenAI

from config import is_admin
from settings import settings

if not settings.debug_bot_token:
    logger.error("DEBUG_BOT_TOKEN not set — debug bot cannot start")
    sys.exit(1)

PROJECT_ROOT = Path("/home/deploy/army-bot")
_ai = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url)
router = Router()


class DebugStates(StatesGroup):
    confirming = State()


# --- helpers ---

def _extract_file_refs(text: str) -> list[tuple[Path, int]]:
    """Parse File "...", line N from Python tracebacks."""
    seen: dict[Path, int] = {}
    for m in re.finditer(r'File "([^"]+)", line (\d+)', text):
        raw, lineno = m.group(1), int(m.group(2))
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.suffix == ".py" and p.exists():
            seen[p] = lineno
    return list(seen.items())


def _read_context(path: Path, line: int, radius: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        start = max(0, line - radius - 1)
        end = min(len(lines), line + radius)
        return "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines[start:end], start=start))
    except Exception as exc:
        return f"[cannot read: {exc}]"


_FIX_PROMPT = """\
You are a senior Python/aiogram developer. Analyze the bug report and propose a minimal fix.

Respond ONLY with valid JSON, no markdown fences:
{
  "file": "relative/path/from/project/root.py",
  "explanation": "root cause and what the fix does (1-2 sentences)",
  "old_code": "exact substring to replace — must match file content character-for-character",
  "new_code": "replacement substring",
  "confidence": "high|medium|low"
}

Rules:
- old_code must be an EXACT copy from the provided context (indentation included)
- Minimal change only — do not refactor surrounding code
- If the fix cannot be expressed as a single replacement, set confidence=low and explain"""


async def _ask_llm(error_text: str, contexts: list[tuple[str, str]]) -> dict:
    ctx_parts = [
        f"=== {rel} ===\n{content}" for rel, content in contexts
    ]
    user_msg = f"BUG REPORT:\n{error_text}\n\nCODE CONTEXT:\n" + "\n\n".join(ctx_parts)
    resp = await _ai.chat.completions.create(
        model="qwen/qwen3-235b-a22b",
        messages=[
            {"role": "system", "content": _FIX_PROMPT},
            {"role": "user", "content": user_msg[:12000]},
        ],
        max_tokens=1200,
    )
    raw = (resp.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    return json.loads(raw)


def _apply_fix(fix: dict) -> tuple[bool, str]:
    filepath = PROJECT_ROOT / fix["file"]
    if not filepath.exists():
        return False, f"File not found: {fix['file']}"

    content = filepath.read_text(encoding="utf-8")
    old_code: str = fix["old_code"]
    new_code: str = fix["new_code"]

    if old_code not in content:
        return False, "old_code not found in file — it may have changed since analysis"

    filepath.write_text(content.replace(old_code, new_code, 1), encoding="utf-8")

    r1 = subprocess.run(
        ["git", "add", fix["file"]], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    r2 = subprocess.run(
        ["git", "commit", "-m", f"fix: {fix.get('explanation', 'debugbot patch')[:72]}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    output = (r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip()
    return True, output


def _guard(user_id: int | None) -> bool:
    return is_admin(user_id)


# --- handlers ---

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _guard(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "🔧 ArmyBots Debug Bot\n\n"
        "Отправьте трейсбек или описание бага — предложу фикс на подтверждение.\n\n"
        "/logs — последние логи сервиса\n"
        "/restart — перезапустить armybots"
    )


@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    if not _guard(message.from_user.id if message.from_user else None):
        return
    r = subprocess.run(
        ["journalctl", "-u", "armybots", "-n", "40", "--no-pager", "--output=cat"],
        capture_output=True, text=True,
    )
    logs = (r.stdout or r.stderr or "(no output)").strip()
    await message.answer(f"```\n{logs[-3800:]}\n```", parse_mode="Markdown")


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    if not _guard(message.from_user.id if message.from_user else None):
        return
    r = subprocess.run(
        ["sudo", "systemctl", "restart", "armybots"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        await message.answer("✅ armybots перезапущен")
    else:
        await message.answer(f"❌ Ошибка рестарта:\n```\n{r.stderr[:800]}\n```", parse_mode="Markdown")


@router.message(F.text, DebugStates.confirming)
async def on_text_while_confirming(message: Message) -> None:
    if not _guard(message.from_user.id if message.from_user else None):
        return
    await message.answer("Сначала ответьте на текущий запрос (Применить / Отмена).")


@router.message(F.text)
async def on_error_report(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id if message.from_user else None):
        return

    text = message.text or ""
    wait = await message.answer("🔍 Анализирую...")

    refs = _extract_file_refs(text)
    if not refs:
        # No traceback — provide recent main.py tail as fallback context
        fallback = PROJECT_ROOT / "main.py"
        refs = [(fallback, 50)] if fallback.exists() else []

    contexts = [
        (str(p.relative_to(PROJECT_ROOT)), _read_context(p, ln))
        for p, ln in refs[:3]
    ]

    try:
        fix = await _ask_llm(text, contexts)
    except json.JSONDecodeError as exc:
        await wait.edit_text(f"❌ LLM вернул некорректный JSON: {exc}")
        return
    except Exception as exc:
        logger.exception("debug_bot: LLM error")
        await wait.edit_text(f"❌ Ошибка LLM: {exc}")
        return

    conf = fix.get("confidence", "?")
    explanation = fix.get("explanation", "")
    file_ = fix.get("file", "")
    old_code = fix.get("old_code", "")[:600]
    new_code = fix.get("new_code", "")[:600]

    preview = (
        f"📁 `{file_}` — уверенность: *{conf}*\n\n"
        f"💬 {explanation}\n\n"
        f"*Убрать:*\n```python\n{old_code}\n```\n"
        f"*Заменить на:*\n```python\n{new_code}\n```"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data="fix:apply"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="fix:cancel"),
    ]])

    await state.set_state(DebugStates.confirming)
    await state.update_data(fix=fix)
    await wait.edit_text(preview, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("fix:"), DebugStates.confirming)
async def on_fix_decision(callback: CallbackQuery, state: FSMContext) -> None:
    action = (callback.data or "").split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.edit_text("❌ Фикс отменён.")
        return

    data = await state.get_data()
    fix = data.get("fix", {})
    await state.clear()

    if callback.message:
        await callback.message.edit_text("⚙️ Применяю фикс...")

    success, output = _apply_fix(fix)

    if success:
        result = (
            f"✅ Фикс применён: `{fix.get('file')}`\n\n"
            f"```\n{output[:1200]}\n```\n\n"
            f"/restart — перезапустить сервис"
        )
    else:
        result = f"❌ Не удалось применить:\n{output}"

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(result, parse_mode="Markdown")


bot = Bot(token=settings.debug_bot_token)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

if __name__ == "__main__":
    logger.info("debug bot: starting")
    asyncio.run(dp.start_polling(bot))
