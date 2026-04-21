"""Project-wide pricing and plan configuration.

Single source of truth for every tier-related number in the codebase. If you
change a price or a limit here, it propagates to billing payments, Telegram
UI buttons, token-bucket enforcement, and the /usage display. Do not inline
any of these numbers anywhere else.
"""
import os
from typing import Any


def _parse_admin_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            continue
    return out


# Comma-separated Telegram user IDs. Admins bypass subscription checks and
# don't have their tokens logged against any plan.
ADMIN_TELEGRAM_IDS: list[int] = _parse_admin_ids(
    os.getenv("ADMIN_TELEGRAM_IDS", "")
)


def is_admin(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in ADMIN_TELEGRAM_IDS


PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "name": "Старт",
        "price_monthly": 490,
        "price_yearly": 4700,
        "bots_limit": 1,
        "tokens_limit": 1_000_000,
        "description": "1 бот, 1М токенов/мес",
    },
    "pro": {
        "name": "Про",
        "price_monthly": 990,
        "price_yearly": 9500,
        "bots_limit": 3,
        "tokens_limit": 5_000_000,
        "description": "3 бота, 5М токенов/мес",
    },
    "business": {
        "name": "Бизнес",
        "price_monthly": 2990,
        "price_yearly": 28700,
        "bots_limit": 10,
        "tokens_limit": None,  # unlimited
        "description": "10 ботов, безлимит токенов",
    },
}

CYCLES: tuple[str, ...] = ("monthly", "yearly")


# Multi-LLM routing — model tiers used by run_bot_query and agents/router.py.
# Keys are the tier names stored in BotConfig.config_json["model_strategy"]
# ("auto" resolves to one of these at runtime); values are OpenRouter slugs.
# Cost per 1M tokens for each slug must also be kept in sync with
# db/repository.MODEL_PRICING_USD_PER_1M.
MODELS: dict[str, str] = {
    "cheap": "meta-llama/llama-3.3-70b-instruct",  # $0.12/1M
    "balanced": "deepseek/deepseek-chat-v3.1",     # $0.28/1M
    "smart": "qwen/qwen3-235b-a22b",               # $0.54/1M
}

MODEL_STRATEGIES: tuple[str, ...] = ("auto", "smart", "cheap")


# Per-container resource caps for generated client bots. Conservative MVP
# defaults — enough to run aiogram polling + a single LLM call in flight,
# tight enough that a runaway bot can't starve the host or its neighbours.
CONTAINER_CPU_LIMIT: float = 0.5
CONTAINER_MEMORY_LIMIT: str = "256m"
