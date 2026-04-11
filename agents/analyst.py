import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pipeline import run_agent

logger = logging.getLogger(__name__)


ANALYST_SYSTEM_PROMPT = """Ты — бизнес-аналитик фабрики Telegram-ботов.
Клиент на русском языке описывает, какой бот ему нужен.
Твоя задача — извлечь из свободного текста структурированные требования.

Верни СТРОГО валидный JSON-объект. Никакого текста до или после JSON.
Никаких markdown-блоков, никаких ```json``` обёрток, никаких пояснений.

Схема ответа:
{
  "bot_type": "parser" | "content" | "sales" | "support" | "other",
  "purpose": "краткое описание цели бота (1-2 предложения)",
  "target_audience": "кто будет использовать бота",
  "key_features": ["фича1", "фича2", ...],
  "tone": "formal" | "friendly" | "professional",
  "language": "ru" | "en",
  "complexity": "simple" | "medium" | "complex"
}

Правила классификации:
- bot_type:
  * "parser"  — собирает/парсит данные из внешних источников
  * "content" — генерирует тексты, посты, картинки, идеи
  * "sales"   — продаёт товары/услуги, принимает заказы, оплаты
  * "support" — отвечает на вопросы клиентов, база знаний, FAQ
  * "other"   — всё, что не укладывается в категории выше
- complexity:
  * "simple"  — до 3 фич, без внешних интеграций
  * "medium"  — 4-7 фич либо 1-2 интеграции
  * "complex" — больше 7 фич или несколько сложных интеграций
- key_features должен содержать минимум один элемент.
- Если клиент не указал язык явно — ставь "ru".
- Если тональность не указана — ставь "friendly"."""


class RequirementsSchema(BaseModel):
    bot_type: Literal["parser", "content", "sales", "support", "other"]
    purpose: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    key_features: list[str] = Field(min_length=1)
    tone: Literal["formal", "friendly", "professional"]
    language: Literal["ru", "en"]
    complexity: Literal["simple", "medium", "complex"]


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def analyst_agent(raw_input: str) -> dict[str, Any]:
    logger.info("analyst_agent: processing %d chars", len(raw_input))
    user_message = f"Описание клиента:\n{raw_input}"

    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = run_agent(system=ANALYST_SYSTEM_PROMPT, user_message=user_message)
        try:
            parsed = json.loads(_strip_fence(raw))
            validated = RequirementsSchema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as err:
            last_error = err
            logger.warning(
                "analyst_agent: invalid output on attempt %d: %s", attempt, err
            )
            user_message = (
                f"Твой предыдущий ответ не прошёл валидацию: {err}\n"
                "Верни СТРОГО валидный JSON по схеме, без markdown-блоков и пояснений.\n\n"
                f"Описание клиента:\n{raw_input}"
            )
            continue

        logger.info(
            "analyst_agent: ok (bot_type=%s, features=%d, complexity=%s)",
            validated.bot_type,
            len(validated.key_features),
            validated.complexity,
        )
        return validated.model_dump()

    raise ValueError(
        f"analyst_agent failed validation after retry: {last_error}"
    ) from last_error
