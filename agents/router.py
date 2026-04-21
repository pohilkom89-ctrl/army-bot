"""Model-routing agent.

choose_model() classifies a user message into one of three tiers
(cheap / balanced / smart) using the cheapest model in MODELS so the
routing overhead itself stays inexpensive. On any LLM or parse error
we fall back to 'balanced' — the bot keeps answering, just without
the cost optimisation for that turn.
"""

import asyncio
import json
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from config import MODELS
from pipeline import run_with_model

ROUTER_SYSTEM_PROMPT = """Ты — маршрутизатор запросов к LLM. \
Проанализируй запрос пользователя и выбери модель:
- "cheap" — простое приветствие, FAQ, короткий ответ
- "balanced" — обычный диалог, средняя задача
- "smart" — сложный анализ, длинная статья, код, творческая задача

Верни ТОЛЬКО JSON: {"model": "cheap|balanced|smart", "reason": "..."}"""


class RouterSchema(BaseModel):
    model: Literal["cheap", "balanced", "smart"]
    reason: str = Field(default="")


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def choose_model(
    user_message: str, context: dict[str, Any]
) -> str:
    """Return one of 'cheap' / 'balanced' / 'smart'. Never raises —
    callers can treat this as infallible and assume 'balanced' is the
    worst-case return value."""
    bot_type = context.get("bot_type", "—")
    user = (
        f"Запрос: {user_message[:1000]}\n"
        f"Тип бота: {bot_type}"
    )
    try:
        raw = await asyncio.to_thread(
            run_with_model,
            MODELS["cheap"],
            ROUTER_SYSTEM_PROMPT,
            user,
        )
        parsed = json.loads(_strip_fence(raw))
        validated = RouterSchema.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as err:
        logger.warning("router: invalid JSON ({}), defaulting to balanced", err)
        return "balanced"
    except Exception:
        logger.exception("router: LLM call failed, defaulting to balanced")
        return "balanced"

    logger.info(
        "router: chose tier={} (reason={!r})",
        validated.model,
        validated.reason[:80],
    )
    return validated.model
