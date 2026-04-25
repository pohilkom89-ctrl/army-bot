"""Fire-and-forget usage reporter shipped into every generated bot's image.

After each LLM call, the generated bot calls report_usage(response.usage, model)
via asyncio.create_task. The factory's /internal/log_tokens endpoint resolves
client_id, looks up cost_usd from MODEL_PRICING_USD_PER_1M, and updates
Subscription.tokens_used. Cost calculation lives in the factory — single
source of truth, no need to redeploy bots when pricing changes.

All failures are swallowed with a loguru warning. The bot must never crash
or hang because the factory is unreachable.
"""

import os
from typing import Any

import aiohttp
from loguru import logger

_FACTORY_URL = os.getenv("FACTORY_URL", "http://host.docker.internal:8080")
_INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
_BOT_ID_RAW = os.getenv("BOT_ID", "")
_BOT_ID = int(_BOT_ID_RAW) if _BOT_ID_RAW.isdigit() else None

_TIMEOUT = aiohttp.ClientTimeout(total=5)
_ENDPOINT = f"{_FACTORY_URL.rstrip('/')}/internal/log_tokens"


async def report_usage(usage: Any, model: str) -> None:
    if _BOT_ID is None:
        logger.warning("usage_reporter: BOT_ID env var missing — skipping")
        return
    if not _INTERNAL_API_KEY:
        logger.warning("usage_reporter: INTERNAL_API_KEY missing — skipping")
        return

    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    if tokens_in is None or tokens_out is None:
        logger.warning(
            "usage_reporter: usage object lacks prompt/completion tokens — skipping"
        )
        return

    payload = {
        "bot_id": _BOT_ID,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "model": model,
    }
    headers = {"X-Internal-Key": _INTERNAL_API_KEY}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_ENDPOINT, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "usage_reporter: factory returned {} — {}",
                        resp.status,
                        body[:200],
                    )
    except Exception as e:
        logger.warning("usage_reporter: POST failed — {}", e)
