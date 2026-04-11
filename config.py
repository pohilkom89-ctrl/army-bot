"""Project-wide pricing and plan configuration.

Single source of truth for every tier-related number in the codebase. If you
change a price or a limit here, it propagates to billing payments, Telegram
UI buttons, token-bucket enforcement, and the /usage display. Do not inline
any of these numbers anywhere else.
"""
from typing import Any

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
