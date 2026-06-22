"""Project-wide pricing and plan configuration.

Single source of truth for every tier-related number in the codebase. If you
change a price or a limit here, it propagates to billing payments, Telegram
UI buttons, token-bucket enforcement, and the /usage display. Do not inline
any of these numbers anywhere else.
"""
from typing import Any

from settings import settings


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
ADMIN_TELEGRAM_IDS: list[int] = _parse_admin_ids(settings.admin_telegram_ids)


def is_admin(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in ADMIN_TELEGRAM_IDS


PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "name": "Старт",
        "price_monthly": 490,
        "price_yearly": 4700,
        "simple_bots_limit": 1,
        "combo_bots_limit": 0,
        "tokens_limit": 1_000_000,
        "merge_limit": 0,
        "multitype_limit": 1,
        "description": "1 простой бот, 1М токенов/мес",
    },
    "pro": {
        "name": "Про",
        "price_monthly": 949,
        "price_yearly": 9500,
        "simple_bots_limit": 2,
        "combo_bots_limit": 2,
        "tokens_limit": 5_000_000,
        "merge_limit": 2,
        "multitype_limit": 2,
        "description": "2 простых + 2 комбо-бота, 5М токенов/мес",
    },
    "business": {
        "name": "Бизнес",
        "price_monthly": 2990,
        "price_yearly": 28700,
        "simple_bots_limit": 5,
        "combo_bots_limit": 3,
        "tokens_limit": None,  # unlimited
        "merge_limit": 5,
        "multitype_limit": 3,
        "description": "5 простых + 3 комбо-бота, безлимит токенов",
    },
}

# Soft token cap for Business tier (tokens/month). At this threshold new
# bot queries are blocked and the owner is prompted to switch to a custom plan.
# Business subscriptions still carry tokens_limit=NULL in the DB; this constant
# is applied at runtime so existing rows need no migration.
BUSINESS_SOFT_CAP: int = 50_000_000

CYCLES: tuple[str, ...] = ("monthly", "yearly")

# Free trial duration for new users. Trial = Pro tier, no payment required,
# single-use per client (tracked via Subscription.plan == "trial").
TRIAL_DAYS: int = 7

# Referral reward: days added to the referrer's subscription when a referred
# user makes their first paid subscription purchase.
REFERRAL_REWARD_DAYS: int = 14


# Multi-LLM routing — model tiers used by run_bot_query and agents/router.py.
# Keys are the tier names stored in BotConfig.config_json["model_strategy"]
# ("auto" resolves to one of these at runtime); values are OpenRouter slugs.
# Cost per 1M tokens for each slug must also be kept in sync with
# db/repository.MODEL_PRICING_USD_PER_1M.
MODELS: dict[str, str] = {
    "cheap": "meta-llama/llama-3.3-70b-instruct",  # $0.12/1M
    "balanced": "deepseek/deepseek-chat-v3.1",     # $0.28/1M
    "smart": "qwen/qwen3-235b-a22b",               # $0.54/1M
    # "yandex:" prefix signals pipeline._chat to use the Yandex client.
    "yandex_lite": "yandex:yandexgpt-lite",        # ~$0.40/1M (RU data center)
    "yandex_pro": "yandex:yandexgpt-pro",          # ~$1.20/1M (RU data center)
}

_yandex_enabled = bool(settings.yandex_api_key and settings.yandex_folder_id)

MODEL_STRATEGIES: tuple[str, ...] = (
    "auto", "smart", "cheap",
    *( ("yandex_lite", "yandex_pro") if _yandex_enabled else () ),
)


# Per-container resource caps for generated client bots. Conservative MVP
# defaults — enough to run aiogram polling + a single LLM call in flight,
# tight enough that a runaway bot can't starve the host or its neighbours.
CONTAINER_CPU_LIMIT: float = 0.5
CONTAINER_MEMORY_LIMIT: str = "256m"
