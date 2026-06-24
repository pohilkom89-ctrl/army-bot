"""Marketplace seller assistant bot runtime.

Works in two modes:
- Connected: WB/Ozon API keys → real orders, stocks, sales data
- Standalone: no API keys → calculators + AI expert advice

Config stored in /app/mp_config.json per user:
{
  "marketplace": "wb" | "ozon" | "both",
  "wb_api_key": "...",       # WB Statistics API token
  "ozon_client_id": "...",   # Ozon seller Client-ID
  "ozon_api_key": "...",     # Ozon seller API-Key
  "stock_alert_days": 14     # alert when days_left < this
}

WB API: https://statistics-api.wildberries.ru/api/v1/supplier/
Ozon API: https://api-seller.ozon.ru/
"""

import asyncio
import json as _json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
CONFIG_FILE = Path("/app/mp_config.json")

openai_client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")

# ── WB/Ozon API constants ────────────────────────────────────────────────────
WB_STATS_BASE = "https://statistics-api.wildberries.ru/api/v1/supplier"
OZON_BASE = "https://api-seller.ozon.ru"

# WB 2026 commission rates by category (approximate)
WB_COMMISSIONS = {
    "clothing": 15,
    "shoes": 12,
    "electronics": 7,
    "household": 13,
    "beauty": 14,
    "food": 10,
    "sports": 13,
    "toys": 12,
    "auto": 10,
    "other": 15,
}

WB_CAT_LABELS = {
    "clothing": "👗 Одежда",
    "shoes": "👟 Обувь",
    "electronics": "📱 Электроника",
    "household": "🏠 Товары для дома",
    "beauty": "💄 Красота и здоровье",
    "food": "🍎 Продукты питания",
    "sports": "⚽ Спорт",
    "toys": "🧸 Игрушки",
    "auto": "🚗 Авто",
    "other": "📦 Другое",
}

OZON_COMMISSIONS = {
    "clothing": 13,
    "shoes": 11,
    "electronics": 6,
    "household": 12,
    "beauty": 13,
    "food": 8,
    "sports": 12,
    "toys": 10,
    "auto": 9,
    "other": 13,
}

# ── Per-user config ──────────────────────────────────────────────────────────
_config: dict[int, dict] = {}


def _load_config() -> None:
    global _config
    try:
        _config = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        _config = {int(k): v for k, v in _config.items()}
    except Exception:
        _config = {}


def _save_config() -> None:
    try:
        CONFIG_FILE.write_text(
            _json.dumps({str(k): v for k, v in _config.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_cfg(uid: int) -> dict:
    if uid not in _config:
        _config[uid] = {}
    return _config[uid]


# ── LLM history ──────────────────────────────────────────────────────────────
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


# ── FSM states ───────────────────────────────────────────────────────────────
class SetupStates(StatesGroup):
    entering_wb_key = State()
    entering_ozon_ids = State()
    entering_ozon_key = State()
    entering_stock_days = State()


class CalcStates(StatesGroup):
    price = State()
    cost = State()
    category = State()
    volume = State()
    returns = State()
    ads = State()


class ReviewStates(StatesGroup):
    waiting_text = State()
    rating = State()


class StockStates(StatesGroup):
    entering_manual = State()


# ── Keyboards ────────────────────────────────────────────────────────────────
def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧮 Калькулятор", callback_data="mp:calc"),
            InlineKeyboardButton(text="📦 Остатки", callback_data="mp:stock"),
        ],
        [
            InlineKeyboardButton(text="📊 Отчёт P&L", callback_data="mp:pnl"),
            InlineKeyboardButton(text="⭐ Ответить на отзыв", callback_data="mp:review"),
        ],
        [
            InlineKeyboardButton(text="🏪 ABC-анализ", callback_data="mp:abc"),
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="mp:alerts"),
        ],
        [
            InlineKeyboardButton(text="🔑 Подключить API", callback_data="mp:setup"),
        ],
    ])


def _marketplace_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data="mp:setup:wb")],
        [InlineKeyboardButton(text="🔵 Ozon", callback_data="mp:setup:ozon")],
        [InlineKeyboardButton(text="🔀 Оба маркетплейса", callback_data="mp:setup:both")],
        [InlineKeyboardButton(text="⏭ Пропустить (только калькулятор)", callback_data="mp:setup:skip")],
    ])


def _category_kb(prefix: str = "calc:cat") -> InlineKeyboardMarkup:
    rows = []
    items = list(WB_CAT_LABELS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, label in items[i:i+2]:
            row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{key}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _skip_kb(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=cb)]
    ])


# ── WB API helpers ───────────────────────────────────────────────────────────
async def _wb_get(endpoint: str, token: str, params: dict = None) -> Optional[dict | list]:
    url = f"{WB_STATS_BASE}/{endpoint}"
    headers = {"Authorization": token}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.warning("wb_api error: {}", e)
    return None


async def _wb_get_stocks(token: str) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    result = await _wb_get("stocks", token, {"dateFrom": date_from})
    return result if isinstance(result, list) else []


async def _wb_get_orders(token: str, days: int = 7) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    result = await _wb_get("orders", token, {"dateFrom": date_from, "flag": 0})
    return result if isinstance(result, list) else []


async def _wb_get_sales(token: str, days: int = 30) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    result = await _wb_get("sales", token, {"dateFrom": date_from, "flag": 0})
    return result if isinstance(result, list) else []


# ── Ozon API helpers ─────────────────────────────────────────────────────────
async def _ozon_post(endpoint: str, client_id: str, api_key: str, body: dict) -> Optional[dict]:
    url = f"{OZON_BASE}{endpoint}"
    headers = {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.warning("ozon_api error: {}", e)
    return None


async def _ozon_get_stocks(client_id: str, api_key: str) -> list[dict]:
    result = await _ozon_post(
        "/v3/product/info/stocks",
        client_id, api_key,
        {"filter": {"has_different_prices": False}, "last_id": "", "limit": 100},
    )
    if result and "result" in result:
        return result["result"].get("items", [])
    return []


async def _ozon_get_orders(client_id: str, api_key: str, days: int = 7) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = await _ozon_post(
        "/v3/posting/fbo/list",
        client_id, api_key,
        {"dir": "DESC", "filter": {"since": date_from, "status": ""}, "limit": 100, "offset": 0, "with": {}},
    )
    if result and "result" in result:
        return result["result"].get("postings", [])
    return []


# ── Unit economics calculator ────────────────────────────────────────────────
def _calc_unit_economics(
    price: float,
    cost: float,
    category: str,
    volume_l: float,
    return_pct: float,
    ads_pct: float,
    marketplace: str = "wb",
) -> dict:
    commissions = WB_COMMISSIONS if marketplace == "wb" else OZON_COMMISSIONS
    commission_pct = commissions.get(category, 15)

    commission_amt = price * commission_pct / 100

    if marketplace == "wb":
        # WB FBO logistics 2026: 46₽ per 1L + 14₽ per additional L
        base_vol = min(volume_l, 1.0)
        extra_vol = max(volume_l - 1.0, 0)
        logistics = 46 * base_vol + 14 * extra_vol
        return_cost = 50  # WB warehouse return
    else:
        # Ozon FBO logistics (approximate)
        logistics = 30 + volume_l * 20
        return_cost = 60

    ads_amt = price * ads_pct / 100
    return_loss = return_pct / 100 * (logistics + return_cost)
    storage = volume_l * 0.5  # approximate daily storage per unit

    profit = price - commission_amt - logistics - ads_amt - return_loss - cost - storage
    margin_pct = (profit / price * 100) if price else 0
    roi = (profit / cost * 100) if cost else 0
    breakeven_price = (cost + logistics + return_loss + storage) / (1 - commission_pct / 100 - ads_pct / 100)

    return {
        "price": price,
        "cost": cost,
        "commission_pct": commission_pct,
        "commission_amt": commission_amt,
        "logistics": logistics,
        "ads_amt": ads_amt,
        "return_loss": return_loss,
        "storage": storage,
        "profit": profit,
        "margin_pct": margin_pct,
        "roi": roi,
        "breakeven_price": breakeven_price,
    }


def _calc_result_text(r: dict, mp_label: str) -> str:
    emoji = "✅" if r["profit"] > 0 else "❌"
    roi_emoji = "🟢" if r["roi"] > 30 else ("🟡" if r["roi"] > 0 else "🔴")
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Результат юнит-экономики* ({mp_label})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена продажи: *{r['price']:.0f} ₽*\n\n"
        f"*Расходы на единицу:*\n"
        f"  • Себестоимость: −{r['cost']:.0f} ₽\n"
        f"  • Комиссия {r['commission_pct']}%: −{r['commission_amt']:.1f} ₽\n"
        f"  • Логистика FBO: −{r['logistics']:.1f} ₽\n"
        f"  • Реклама {r['ads_amt']/r['price']*100:.0f}%: −{r['ads_amt']:.1f} ₽\n"
        f"  • Потери от возвратов: −{r['return_loss']:.1f} ₽\n"
        f"  • Хранение: −{r['storage']:.1f} ₽\n\n"
        f"{emoji} *Прибыль: {r['profit']:.1f} ₽*\n"
        f"📈 Маржинальность: *{r['margin_pct']:.1f}%*\n"
        f"{roi_emoji} ROI: *{r['roi']:.0f}%*\n"
        f"⚖️ Точка безубыточности: *{r['breakeven_price']:.0f} ₽*\n\n"
        f"{'💡 _Рекомендация: продавать выгодно_' if r['profit'] > 0 else '⚠️ _Рекомендация: пересмотреть ценообразование или снизить себестоимость_'}"
    )


# ── Stock analysis ────────────────────────────────────────────────────────────
def _analyze_stocks(stocks: list[dict], marketplace: str) -> str:
    if not stocks:
        return "Нет данных по остаткам"

    if marketplace == "wb":
        total_qty = sum(s.get("quantity", 0) for s in stocks)
        items_by_barcode: dict[str, int] = {}
        for s in stocks:
            bc = str(s.get("barcode", ""))
            items_by_barcode[bc] = items_by_barcode.get(bc, 0) + s.get("quantity", 0)
        low_stock = [(bc, qty) for bc, qty in items_by_barcode.items() if qty < 10]
        lines = [f"📦 *Остатки WB* (всего SKU: {len(items_by_barcode)})", f"Общее кол-во: *{total_qty} шт.*", ""]
        if low_stock:
            lines.append(f"⚠️ *Заканчиваются ({len(low_stock)} SKU):*")
            for bc, qty in sorted(low_stock, key=lambda x: x[1])[:10]:
                lines.append(f"  • ...{bc[-6:]}: {qty} шт.")
        return "\n".join(lines)
    else:
        items = [i for i in stocks if isinstance(i, dict)]
        total = sum(
            sum(w.get("present", 0) for w in i.get("stocks", []))
            for i in items
        )
        return f"📦 *Остатки Ozon* (SKU: {len(items)})\nОбщее кол-во: *{total} шт.*"


# ── Orders analysis ───────────────────────────────────────────────────────────
def _analyze_orders(orders: list[dict], marketplace: str, days: int) -> str:
    if not orders:
        return f"Заказов за {days} дней не найдено"

    if marketplace == "wb":
        total = len(orders)
        cancelled = sum(1 for o in orders if o.get("isCancel", False))
        revenue = sum(o.get("totalPrice", 0) for o in orders if not o.get("isCancel", False))
        return (
            f"📋 *Заказы WB за {days} дней*\n"
            f"Всего: *{total}* шт.\n"
            f"Отменено: {cancelled} шт.\n"
            f"Выкупленные: *{total - cancelled}* шт.\n"
            f"Оборот: *{revenue:,.0f} ₽*\n"
            f"Средний чек: *{revenue / max(total - cancelled, 1):,.0f} ₽*"
        )
    else:
        total = len(orders)
        return f"📋 *Заказы Ozon за {days} дней*\nВсего: *{total}* шт."


# ── ABC analysis ──────────────────────────────────────────────────────────────
def _abc_analysis(sales: list[dict]) -> str:
    if not sales:
        return "Недостаточно данных для ABC-анализа"

    # Group by nm_id (WB article)
    items: dict[int, dict] = {}
    for s in sales:
        nid = s.get("nmId", 0)
        if nid not in items:
            items[nid] = {"name": s.get("supplierArticle", str(nid)), "revenue": 0, "qty": 0}
        price = s.get("totalPrice", 0)
        items[nid]["revenue"] += price
        items[nid]["qty"] += 1

    sorted_items = sorted(items.values(), key=lambda x: x["revenue"], reverse=True)
    total_rev = sum(i["revenue"] for i in sorted_items)

    lines = ["🏪 *ABC-анализ продаж (30 дней)*\n"]
    cumulative = 0
    for i, item in enumerate(sorted_items[:20]):
        cumulative += item["revenue"]
        pct_cumulative = cumulative / total_rev * 100 if total_rev else 0
        group = "A" if pct_cumulative <= 80 else ("B" if pct_cumulative <= 95 else "C")
        emoji = {"A": "🟢", "B": "🟡", "C": "🔴"}[group]
        lines.append(f"{emoji} [{group}] {item['name'][:20]}: {item['revenue']:,.0f} ₽ ({item['qty']} шт.)")

    lines.append(f"\n_Всего оборот: {total_rev:,.0f} ₽_")
    return "\n".join(lines)


# ── Bot & Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    asyncio.create_task(report_subscriber(uid))
    cfg = _get_cfg(uid)

    if cfg.get("marketplace"):
        mp = cfg["marketplace"]
        mp_label = {"wb": "Wildberries", "ozon": "Ozon", "both": "WB + Ozon"}.get(mp, mp)
        connected = "✅ API подключён" if cfg.get("wb_api_key") or cfg.get("ozon_api_key") else "⚡ Режим калькулятора"
        await message.answer(
            f"👋 С возвращением!\n\n"
            f"🏪 Маркетплейс: *{mp_label}*\n"
            f"{connected}\n\n"
            f"Выберите действие:",
            reply_markup=_main_menu_kb(),
            parse_mode="Markdown",
        )
    else:
        await message.answer(
            "🏪 *Ассистент продавца маркетплейсов*\n\n"
            "Я помогаю селлерам на WB и Ozon:\n"
            "🧮 Считаю юнит-экономику с актуальными тарифами 2026\n"
            "📦 Контролирую остатки и прогнозирую поставки\n"
            "⭐ Генерирую ответы на отзывы через AI\n"
            "📊 Строю ABC-анализ ассортимента\n"
            "🔔 Уведомляю о критических остатках\n\n"
            "С какого маркетплейса начнём?",
            reply_markup=_marketplace_choice_kb(),
            parse_mode="Markdown",
        )


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Главное меню:", reply_markup=_main_menu_kb())


@dp.message(Command("calc"))
async def cmd_calc(message: Message, state: FSMContext) -> None:
    await _start_calc(message, state)


@dp.message(Command("stock"))
async def cmd_stock(message: Message, state: FSMContext) -> None:
    await _show_stock(message.from_user.id, message.chat.id)


@dp.message(Command("review"))
async def cmd_review(message: Message, state: FSMContext) -> None:
    await _start_review(message, state)


@dp.message(Command("pnl"))
async def cmd_pnl(message: Message) -> None:
    await _show_pnl(message.from_user.id, message.chat.id)


@dp.message(Command("abc"))
async def cmd_abc(message: Message) -> None:
    uid = message.from_user.id
    cfg = _get_cfg(uid)
    wb_key = cfg.get("wb_api_key")
    if not wb_key:
        await message.answer("Для ABC-анализа подключите WB API. Введите /start → 🔑 Подключить API")
        return
    await message.answer("⏳ Загружаю данные продаж за 30 дней...")
    sales = await _wb_get_sales(wb_key, days=30)
    await message.answer(_abc_analysis(sales), parse_mode="Markdown")


# ── Setup callbacks ───────────────────────────────────────────────────────────
@dp.callback_query(F.data == "mp:setup")
async def cb_setup(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.answer(
        "🔑 *Подключение API маркетплейса*\n\nВыберите площадку:",
        reply_markup=_marketplace_choice_kb(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mp:setup:"))
async def cb_setup_choice(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    mp = callback.data.split(":")[2]

    if mp == "skip":
        _get_cfg(uid)["marketplace"] = "wb"
        _save_config()
        await callback.message.answer(
            "✅ Режим калькулятора активирован!\n\n"
            "API не подключён — доступны: калькулятор юнит-экономики, генератор ответов на отзывы, AI-советник.\n\n"
            "Чтобы получить данные заказов/остатков — подключите API позже через 🔑 Подключить API.",
            reply_markup=_main_menu_kb(),
        )
        await callback.answer()
        return

    _get_cfg(uid)["marketplace"] = mp
    _save_config()

    if mp in ("wb", "both"):
        await callback.message.answer(
            "🟣 *Wildberries API*\n\n"
            "1. Откройте *Личный кабинет WB* → Профиль → Настройки → Доступ к API\n"
            "2. Создайте ключ с правами: *Статистика* (обязательно), *Контент*, *Аналитика*\n"
            "3. Скопируйте токен и отправьте его сюда\n\n"
            "Или нажмите «Пропустить» чтобы продолжить без API:",
            reply_markup=_skip_kb("mp:skip_wb"),
            parse_mode="Markdown",
        )
        await state.set_state(SetupStates.entering_wb_key)
    elif mp == "ozon":
        await callback.message.answer(
            "🔵 *Ozon API*\n\n"
            "1. Откройте *Личный кабинет Ozon* → Настройки → API-ключи\n"
            "2. Нажмите «Создать ключ» → тип *Admin read-only* или *Admin*\n"
            "3. Скопируйте *Client-ID* и *Api-Key*\n\n"
            "Отправьте Client-ID (только цифры):",
            reply_markup=_skip_kb("mp:skip_ozon"),
            parse_mode="Markdown",
        )
        await state.set_state(SetupStates.entering_ozon_ids)
    await callback.answer()


@dp.callback_query(F.data == "mp:skip_wb")
async def cb_skip_wb(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    mp = _get_cfg(uid).get("marketplace", "wb")
    if mp == "both":
        await callback.message.answer(
            "🔵 Теперь Ozon. Отправьте Client-ID (только цифры):",
            reply_markup=_skip_kb("mp:skip_ozon"),
        )
        await state.set_state(SetupStates.entering_ozon_ids)
    else:
        await state.clear()
        await callback.message.answer("✅ Готово! API не подключён — работаем в режиме калькулятора.", reply_markup=_main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "mp:skip_ozon")
async def cb_skip_ozon(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("✅ Настройка завершена!", reply_markup=_main_menu_kb())
    await callback.answer()


@dp.message(SetupStates.entering_wb_key)
async def on_wb_key(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    token = message.text.strip()
    await message.answer("⏳ Проверяю ключ...")

    # Quick validation: try fetching stocks
    stocks = await _wb_get_stocks(token)
    if stocks is not None:
        _get_cfg(uid)["wb_api_key"] = token
        _save_config()
        mp = _get_cfg(uid).get("marketplace", "wb")
        if mp == "both":
            await message.answer(
                "✅ WB API подключён!\n\nТеперь введите Ozon Client-ID:",
                reply_markup=_skip_kb("mp:skip_ozon"),
            )
            await state.set_state(SetupStates.entering_ozon_ids)
        else:
            await state.clear()
            await message.answer(
                f"✅ WB API подключён! Найдено {len(stocks)} позиций на складе.\n\nГотово!",
                reply_markup=_main_menu_kb(),
            )
    else:
        await message.answer(
            "❌ Не удалось проверить ключ. Убедитесь что включены права *Статистика* и попробуйте снова:",
            parse_mode="Markdown",
        )


@dp.message(SetupStates.entering_ozon_ids)
async def on_ozon_client_id(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    cid = message.text.strip()
    if not cid.isdigit():
        await message.answer("Client-ID должен состоять только из цифр. Попробуйте ещё раз:")
        return
    await state.update_data(ozon_client_id=cid)
    await message.answer(
        "✅ Client-ID принят. Теперь отправьте Api-Key (длинная строка):",
        reply_markup=_skip_kb("mp:skip_ozon"),
    )
    await state.set_state(SetupStates.entering_ozon_key)


@dp.message(SetupStates.entering_ozon_key)
async def on_ozon_api_key(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    data = await state.get_data()
    cid = data.get("ozon_client_id", "")
    api_key = message.text.strip()
    await message.answer("⏳ Проверяю ключ Ozon...")

    stocks = await _ozon_get_stocks(cid, api_key)
    if stocks is not None:
        cfg = _get_cfg(uid)
        cfg["ozon_client_id"] = cid
        cfg["ozon_api_key"] = api_key
        _save_config()
        await state.clear()
        await message.answer(
            f"✅ Ozon API подключён! Найдено {len(stocks)} позиций.\n\nВсё готово!",
            reply_markup=_main_menu_kb(),
        )
    else:
        await message.answer("❌ Не удалось проверить ключ. Попробуйте ещё раз:")


# ── Main menu callbacks ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "mp:calc")
async def cb_calc(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_calc(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "mp:stock")
async def cb_stock(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    await _show_stock(uid, callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "mp:pnl")
async def cb_pnl(callback: CallbackQuery) -> None:
    await _show_pnl(callback.from_user.id, callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "mp:review")
async def cb_review(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_review(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "mp:abc")
async def cb_abc(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    cfg = _get_cfg(uid)
    wb_key = cfg.get("wb_api_key")
    if not wb_key:
        await callback.message.answer("Для ABC-анализа нужен WB API. Подключите через 🔑 Подключить API.")
    else:
        await callback.message.answer("⏳ Загружаю данные продаж...")
        sales = await _wb_get_sales(wb_key, days=30)
        await callback.message.answer(_abc_analysis(sales), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "mp:alerts")
async def cb_alerts(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    cfg = _get_cfg(uid)
    days = cfg.get("stock_alert_days", 14)
    enabled = cfg.get("alerts_enabled", True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if enabled else '❌'} Уведомления об остатках",
            callback_data="mp:alert:toggle"
        )],
        [InlineKeyboardButton(text=f"📅 Порог: {days} дней → изменить", callback_data="mp:alert:days")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mp:menu")],
    ])
    await callback.message.answer(
        f"🔔 *Настройки уведомлений*\n\n"
        f"Я уведомлю когда запас товара закончится менее чем через *{days} дней*.\n"
        f"Ежедневный дайджест: *{'включён' if enabled else 'выключен'}* (09:00 МСК)",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "mp:alert:toggle")
async def cb_alert_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    cfg = _get_cfg(uid)
    cfg["alerts_enabled"] = not cfg.get("alerts_enabled", True)
    _save_config()
    state_label = "включены ✅" if cfg["alerts_enabled"] else "выключены ❌"
    await callback.answer(f"Уведомления {state_label}", show_alert=True)


@dp.callback_query(F.data == "mp:alert:days")
async def cb_alert_days(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.answer(
        "Введите количество дней (например *14*).\n"
        "Я уведомлю когда остатка хватит менее чем на столько дней:",
        parse_mode="Markdown",
    )
    await state.set_state(SetupStates.entering_stock_days)
    await callback.answer()


@dp.message(SetupStates.entering_stock_days)
async def on_stock_days(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    try:
        days = int(message.text.strip())
        if days < 1 or days > 365:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 365:")
        return
    _get_cfg(uid)["stock_alert_days"] = days
    _save_config()
    await state.clear()
    await message.answer(f"✅ Порог установлен: {days} дней", reply_markup=_main_menu_kb())


@dp.callback_query(F.data == "mp:menu")
async def cb_menu(callback: CallbackQuery) -> None:
    await callback.message.answer("Главное меню:", reply_markup=_main_menu_kb())
    await callback.answer()


# ── Unit economics calculator FSM ─────────────────────────────────────────────
async def _start_calc(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    mp = _get_cfg(uid).get("marketplace", "wb")
    mp_label = {"wb": "Wildberries", "ozon": "Ozon", "both": "WB"}.get(mp, "WB")
    await state.set_state(CalcStates.price)
    await message.answer(
        f"🧮 *Калькулятор юнит-экономики {mp_label}*\n\n"
        f"Тарифы актуальны на 2026 год.\n\n"
        f"Шаг 1/6 — Введите *цену продажи* (₽):",
        parse_mode="Markdown",
    )


@dp.message(CalcStates.price)
async def calc_price(message: Message, state: FSMContext) -> None:
    try:
        price = float(message.text.strip().replace(",", ".").replace(" ", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число:")
        return
    await state.update_data(price=price)
    await state.set_state(CalcStates.cost)
    await message.answer(f"✅ Цена: {price:.0f} ₽\n\nШаг 2/6 — Введите *себестоимость* (₽, закупка + упаковка + маркировка):", parse_mode="Markdown")


@dp.message(CalcStates.cost)
async def calc_cost(message: Message, state: FSMContext) -> None:
    try:
        cost = float(message.text.strip().replace(",", ".").replace(" ", ""))
        if cost < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите неотрицательное число:")
        return
    await state.update_data(cost=cost)
    await state.set_state(CalcStates.category)
    uid = message.from_user.id
    mp = _get_cfg(uid).get("marketplace", "wb")
    await message.answer(
        f"✅ Себестоимость: {cost:.0f} ₽\n\nШаг 3/6 — Выберите *категорию товара*:",
        reply_markup=_category_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("calc:cat:"))
async def calc_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":")[2]
    uid = callback.from_user.id
    mp = _get_cfg(uid).get("marketplace", "wb")
    commissions = WB_COMMISSIONS if mp != "ozon" else OZON_COMMISSIONS
    comm_pct = commissions.get(category, 15)
    await state.update_data(category=category)
    await state.set_state(CalcStates.volume)
    await callback.message.answer(
        f"✅ Категория: {WB_CAT_LABELS.get(category, category)} (комиссия {comm_pct}%)\n\n"
        f"Шаг 4/6 — Введите *объём* упаковки в литрах (Д×Ш×В см ÷ 1000)\n"
        f"Пример: коробка 20×15×10 см = 3 литра",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(CalcStates.volume)
async def calc_volume(message: Message, state: FSMContext) -> None:
    try:
        vol = float(message.text.strip().replace(",", ".").replace(" ", ""))
        if vol <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите объём в литрах (положительное число):")
        return
    await state.update_data(volume=vol)
    await state.set_state(CalcStates.returns)
    await message.answer(
        f"✅ Объём: {vol} л\n\nШаг 5/6 — Введите *процент возвратов* (%):\n"
        f"_Обычно: одежда 30-50%, электроника 5-10%, товары для дома 10-15%_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="5%", callback_data="calc:ret:5"),
             InlineKeyboardButton(text="10%", callback_data="calc:ret:10"),
             InlineKeyboardButton(text="20%", callback_data="calc:ret:20"),
             InlineKeyboardButton(text="30%", callback_data="calc:ret:30")],
        ]),
    )


@dp.callback_query(F.data.startswith("calc:ret:"))
async def calc_return_btn(callback: CallbackQuery, state: FSMContext) -> None:
    ret = int(callback.data.split(":")[2])
    await state.update_data(returns=float(ret))
    await state.set_state(CalcStates.ads)
    await callback.message.answer(
        f"✅ Возвраты: {ret}%\n\nШаг 6/6 — Введите *расходы на рекламу* (% от цены):\n"
        f"_Рекомендуется закладывать 5-15% для новых товаров_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="5%", callback_data="calc:ads:5"),
             InlineKeyboardButton(text="10%", callback_data="calc:ads:10"),
             InlineKeyboardButton(text="15%", callback_data="calc:ads:15"),
             InlineKeyboardButton(text="0% (нет рекламы)", callback_data="calc:ads:0")],
        ]),
    )
    await callback.answer()


@dp.message(CalcStates.returns)
async def calc_returns_text(message: Message, state: FSMContext) -> None:
    try:
        ret = float(message.text.strip().replace(",", ".").replace("%", ""))
        if ret < 0 or ret > 100:
            raise ValueError
    except ValueError:
        await message.answer("Введите процент от 0 до 100:")
        return
    await state.update_data(returns=ret)
    await state.set_state(CalcStates.ads)
    await message.answer(
        f"✅ Возвраты: {ret}%\n\nШаг 6/6 — Введите *расходы на рекламу* (% от цены):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="5%", callback_data="calc:ads:5"),
             InlineKeyboardButton(text="10%", callback_data="calc:ads:10"),
             InlineKeyboardButton(text="15%", callback_data="calc:ads:15"),
             InlineKeyboardButton(text="0%", callback_data="calc:ads:0")],
        ]),
    )


@dp.callback_query(F.data.startswith("calc:ads:"))
async def calc_ads_btn(callback: CallbackQuery, state: FSMContext) -> None:
    ads = int(callback.data.split(":")[2])
    await _finish_calc(callback.message, callback.from_user.id, state, float(ads))
    await callback.answer()


@dp.message(CalcStates.ads)
async def calc_ads_text(message: Message, state: FSMContext) -> None:
    try:
        ads = float(message.text.strip().replace(",", ".").replace("%", ""))
        if ads < 0 or ads > 100:
            raise ValueError
    except ValueError:
        await message.answer("Введите процент от 0 до 100:")
        return
    await _finish_calc(message, message.from_user.id, state, ads)


async def _finish_calc(message: Message, uid: int, state: FSMContext, ads_pct: float) -> None:
    data = await state.get_data()
    await state.clear()
    mp = _get_cfg(uid).get("marketplace", "wb")
    mp_label = {"wb": "Wildberries", "ozon": "Ozon", "both": "Wildberries"}.get(mp, "WB")

    result = _calc_unit_economics(
        price=data["price"],
        cost=data["cost"],
        category=data["category"],
        volume_l=data["volume"],
        return_pct=data["returns"],
        ads_pct=ads_pct,
        marketplace="wb" if mp != "ozon" else "ozon",
    )
    text = _calc_result_text(result, mp_label)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пересчитать", callback_data="mp:calc")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="mp:menu")],
    ])
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


# ── Review reply generator ────────────────────────────────────────────────────
async def _start_review(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐1", callback_data="rv:rating:1"),
         InlineKeyboardButton(text="⭐⭐2", callback_data="rv:rating:2"),
         InlineKeyboardButton(text="⭐⭐⭐3", callback_data="rv:rating:3"),
         InlineKeyboardButton(text="⭐⭐⭐⭐4", callback_data="rv:rating:4"),
         InlineKeyboardButton(text="⭐⭐⭐⭐⭐5", callback_data="rv:rating:5")],
    ])
    await state.set_state(ReviewStates.rating)
    await message.answer(
        "⭐ *Генератор ответов на отзывы*\n\nКакая оценка у отзыва?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("rv:rating:"))
async def cb_review_rating(callback: CallbackQuery, state: FSMContext) -> None:
    rating = int(callback.data.split(":")[2])
    await state.update_data(rating=rating)
    await state.set_state(ReviewStates.waiting_text)
    mood = "негативный" if rating <= 2 else ("нейтральный" if rating == 3 else "положительный")
    await callback.message.answer(
        f"Оценка: {'⭐' * rating} ({mood})\n\n"
        f"Вставьте текст отзыва покупателя:"
    )
    await callback.answer()


@dp.message(ReviewStates.waiting_text)
async def on_review_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rating = data.get("rating", 5)
    review_text = message.text.strip()
    await state.clear()
    uid = message.from_user.id
    cfg = _get_cfg(uid)
    mp = cfg.get("marketplace", "wb")

    await message.answer("✍️ Генерирую ответ...")

    stars = "⭐" * rating
    mood_instruction = (
        "Отзыв негативный. Принеси искренние извинения, предложи решение проблемы, попроси связаться через поддержку маркетплейса."
        if rating <= 2 else
        "Отзыв нейтральный или смешанный. Поблагодари за честную обратную связь, объясни нюансы если нужно, пригласи снова."
        if rating == 3 else
        "Отзыв положительный. Искренне поблагодари, подчеркни преимущество товара упомянутое в отзыве, пригласи вернуться."
    )

    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Ты помогаешь продавцу на маркетплейсе написать ответ на отзыв покупателя.\n"
        f"Правила:\n"
        f"- Ответ не более 3-4 предложений\n"
        f"- Не используй шаблонные фразы типа 'Мы рады вашему отзыву'\n"
        f"- Отвечай от лица продавца (не магазина)\n"
        f"- {mood_instruction}\n"
        f"- НЕ упоминай скидки и подарки (нарушение правил WB/Ozon)\n"
        f"- Не добавляй смайлики в избытке"
    )

    prompt = f"Отзыв покупателя (оценка {stars}):\n{review_text}\n\nНапиши ответ продавца:"
    try:
        resp = await openai_client.chat.completions.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        asyncio.create_task(report_usage(resp.usage, MODEL))
        reply_text = resp.choices[0].message.content or ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data="mp:review")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="mp:menu")],
        ])
        await message.answer(
            f"*Ответ на отзыв {stars}:*\n\n{reply_text}",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception:
        logger.exception("marketplace: LLM review failed uid={}", uid)
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")


# ── P&L / Stock helpers ───────────────────────────────────────────────────────
async def _show_stock(uid: int, chat_id: int) -> None:
    cfg = _get_cfg(uid)
    wb_key = cfg.get("wb_api_key")
    ozon_cid = cfg.get("ozon_client_id")
    ozon_key = cfg.get("ozon_api_key")

    if not wb_key and not ozon_cid:
        await bot.send_message(
            chat_id,
            "📦 Для просмотра остатков подключите API маркетплейса.\n"
            "Используйте 🔑 Подключить API в главном меню.",
            reply_markup=_main_menu_kb(),
        )
        return

    await bot.send_message(chat_id, "⏳ Загружаю данные об остатках...")

    if wb_key:
        stocks = await _wb_get_stocks(wb_key)
        await bot.send_message(chat_id, _analyze_stocks(stocks, "wb"), parse_mode="Markdown")

    if ozon_cid and ozon_key:
        stocks = await _ozon_get_stocks(ozon_cid, ozon_key)
        await bot.send_message(chat_id, _analyze_stocks(stocks, "ozon"), parse_mode="Markdown")


async def _show_pnl(uid: int, chat_id: int) -> None:
    cfg = _get_cfg(uid)
    wb_key = cfg.get("wb_api_key")
    ozon_cid = cfg.get("ozon_client_id")
    ozon_key = cfg.get("ozon_api_key")

    if not wb_key and not ozon_cid:
        await bot.send_message(
            chat_id,
            "📊 Для P&L-отчёта подключите API маркетплейса.",
            reply_markup=_main_menu_kb(),
        )
        return

    await bot.send_message(chat_id, "⏳ Формирую P&L-отчёт за 30 дней...")

    if wb_key:
        orders_7 = await _wb_get_orders(wb_key, days=7)
        orders_30 = await _wb_get_orders(wb_key, days=30)
        sales_30 = await _wb_get_sales(wb_key, days=30)

        rev_30 = sum(s.get("totalPrice", 0) for s in sales_30)
        rev_7 = sum(o.get("totalPrice", 0) for o in orders_7 if not o.get("isCancel"))
        orders_count_30 = len([o for o in orders_30 if not o.get("isCancel")])
        orders_count_7 = len([o for o in orders_7 if not o.get("isCancel")])
        avg_check = rev_30 / max(len(sales_30), 1)

        today = datetime.now()
        text = (
            f"📊 *P&L Wildberries*\n"
            f"_{today.strftime('%d.%m.%Y')}_\n\n"
            f"*За 7 дней:*\n"
            f"  Заказов: *{orders_count_7}*\n"
            f"  Оборот: *{rev_7:,.0f} ₽*\n\n"
            f"*За 30 дней:*\n"
            f"  Продаж: *{len(sales_30)}*\n"
            f"  Оборот: *{rev_30:,.0f} ₽*\n"
            f"  Заказов: *{orders_count_30}*\n"
            f"  Средний чек: *{avg_check:,.0f} ₽*\n\n"
            f"_⚠️ Данные от WB Statistics API. Чистая прибыль рассчитывается через /calc_"
        )
        await bot.send_message(chat_id, text, parse_mode="Markdown")

    if ozon_cid and ozon_key:
        orders = await _ozon_get_orders(ozon_cid, ozon_key, days=30)
        await bot.send_message(
            chat_id,
            f"📊 *P&L Ozon (30 дней)*\n\nЗаказов: *{len(orders)}*",
            parse_mode="Markdown",
        )


# ── Daily digest scheduler ───────────────────────────────────────────────────
async def _send_daily_digest() -> None:
    for uid, cfg in _config.items():
        if not cfg.get("alerts_enabled", True):
            continue
        wb_key = cfg.get("wb_api_key")
        if not wb_key:
            continue
        threshold = cfg.get("stock_alert_days", 14)
        try:
            stocks = await _wb_get_stocks(wb_key)
            orders_7 = await _wb_get_orders(wb_key, days=7)

            if not stocks:
                continue

            # Find low stock items
            items_qty: dict[str, int] = {}
            for s in stocks:
                bc = str(s.get("barcode", ""))
                items_qty[bc] = items_qty.get(bc, 0) + s.get("quantity", 0)

            orders_count = len([o for o in orders_7 if not o.get("isCancel")])
            sales_per_day = orders_count / 7 if orders_count else 0

            low_stock_alerts = []
            for bc, qty in items_qty.items():
                days_left = qty / sales_per_day if sales_per_day > 0 else 999
                if days_left < threshold:
                    low_stock_alerts.append((bc, qty, int(days_left)))

            rev_7 = sum(o.get("totalPrice", 0) for o in orders_7 if not o.get("isCancel"))
            today = datetime.now()

            lines = [
                f"🌅 *Доброе утро! Дайджест WB — {today.strftime('%d.%m.%Y')}*\n",
                f"📋 Заказов за 7 дней: *{orders_count}*",
                f"💰 Оборот за 7 дней: *{rev_7:,.0f} ₽*",
                f"📦 Артикулов на складе: *{len(items_qty)}*",
            ]

            if low_stock_alerts:
                lines.append(f"\n⚠️ *Заканчиваются ({len(low_stock_alerts)} SKU):*")
                for bc, qty, days_left in sorted(low_stock_alerts, key=lambda x: x[2])[:5]:
                    lines.append(f"  • ...{bc[-6:]}: {qty} шт. (~{days_left} дней)")
                lines.append("\n💡 Пора планировать поставку!")

            await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown", reply_markup=_main_menu_kb())
        except Exception:
            logger.exception("marketplace: daily digest failed uid={}", uid)


# ── Fallback: LLM for general questions ──────────────────────────────────────
@dp.message()
async def on_message(message: Message) -> None:
    if not message.text:
        return
    uid = message.from_user.id
    text = message.text.strip()
    await _ensure_history_loaded(uid)
    asyncio.create_task(report_message(uid, str(uid), "user", text))
    _append_history(uid, "user", text)

    marketplace_context = (
        f"\n\nПользователь — продавец на маркетплейсах. "
        f"Отвечай как эксперт по WB/Ozon: юнит-экономика, SEO карточек, работа с отзывами, "
        f"складская логистика, реклама на маркетплейсах, анализ конкурентов."
    )
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + marketplace_context}] + _get_history(uid)
    try:
        resp = await openai_client.chat.completions.create(
            model=MODEL, max_tokens=1024, messages=msgs
        )
        asyncio.create_task(report_usage(resp.usage, MODEL))
        reply = resp.choices[0].message.content or ""
        _append_history(uid, "assistant", reply)
        asyncio.create_task(report_message(uid, str(uid), "bot", reply))
        await message.answer(reply, reply_markup=_main_menu_kb())
    except Exception:
        logger.exception("marketplace: LLM failed uid={}", uid)
        await message.answer("Произошла ошибка. Попробуйте позже.")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    _load_config()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(_send_daily_digest, "cron", hour=9, minute=0)
    scheduler.start()
    logger.info("Marketplace bot starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
