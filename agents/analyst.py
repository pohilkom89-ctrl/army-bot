import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pipeline import run_agent

logger = logging.getLogger(__name__)


ANALYST_SYSTEM_PROMPT = """Ты — бизнес-аналитик фабрики Telegram-ботов.
Клиент выбрал тип бота и ответил на анкету из 10-13 конкретных вопросов.
Твоя задача — извлечь из ответов структурированные требования и type-specific данные.

Верни СТРОГО валидный JSON-объект. Никакого текста до или после JSON.
Никаких markdown-блоков, никаких ```json``` обёрток, никаких пояснений.

Схема ответа:
{
  "bot_type": "parser" | "seller" | "content" | "support",
  "purpose": "краткое описание цели бота (1-2 предложения)",
  "target_audience": "кто будет использовать бота",
  "key_features": ["фича1", "фича2", ...],
  "tone": "formal" | "friendly" | "professional",
  "language": "ru" | "en",
  "complexity": "simple" | "medium" | "complex",
  "extras": { ...type-specific структурированные данные... }
}

Правила классификации bot_type:
- "parser"  — собирает/парсит данные из внешних источников (VK, TG, Instagram)
- "seller"  — продаёт товары/услуги, принимает заказы
- "content" — генерирует тексты, посты, статьи
- "support" — отвечает на вопросы клиентов, работает с базой знаний/FAQ

Правила по complexity:
- "simple"  — до 3 фич, без внешних интеграций
- "medium"  — 4-7 фич либо 1-2 интеграции
- "complex" — больше 7 фич или несколько сложных интеграций

Правила по key_features:
- минимум один элемент
- формулируй как конкретные действия бота («парсит VK-группы ежедневно», «принимает заявки в Telegram менеджера»)
- НЕ включай в key_features секреты, API-токены или контактные данные

Правила по extras (type-specific структурирование ответов клиента):

- Для parser:
  {
    "vk_sources": ["vk.com/...", ...],          // из вопроса про VK группы
    "telegram_sources": ["@channel1", ...],     // из вопроса про TG каналы
    "keywords": ["слово1", ...],                // из вопроса про ключевые слова
    "niche": "ниша бизнеса",
    "report_frequency": "daily | weekly | on_demand",
    "report_format": "table | text | top5 | ...",
    "has_vk_token": true | false,               // true если клиент указал токен (значение НЕ включай!)
    "generate_articles": true | false,
    "article_style": "...",
    "article_length_words": число | null,
    "publish_to": "..."
  }

- Для seller:
  {
    "company": "название и сфера",
    "products": [{"name": "...", "price": "..."}],  // из списка «товар — цена»
    "goal": "заявка | консультация | онлайн-продажа",
    "faq": [{"q": "...", "a": "..."}],              // если клиент дал пары
    "manager_telegram_placeholder": true | false,   // true если клиент указал контакт (сам контакт НЕ пиши!)
    "delivery_and_payment": "...",
    "discounts": "...",
    "tone": "official | friendly | expert",
    "human_fallback": "switch_to_manager | give_contacts",
    "working_hours": "...",
    "warranty_return": "...",
    "website": "..."
  }

- Для content:
  {
    "niche": "...",
    "audience": "...",
    "content_types": ["post_vk", "article", "stories", "email"],
    "tone_style": "...",
    "example_texts": "...",
    "forbidden_topics": ["..."],
    "post_length": "short | medium | long",
    "use_hashtags": true | false,
    "hashtag_themes": ["..."],
    "frequency": "on_demand | scheduled",
    "publish_to": "...",
    "publish_channel_placeholder": true | false,
    "competitors": ["..."]
  }

- Для support:
  {
    "company": "...",
    "faq": [{"q": "...", "a": "..."}],           // разбери базу знаний на пары
    "top_issues": ["проблема 1", ...],
    "unknown_answer_strategy": "switch_to_human | ask_to_wait",
    "manager_telegram_placeholder": true | false,
    "working_hours": "...",
    "tone": "official | friendly",
    "forbidden_topics": ["..."],
    "collect_contacts": ["name", "phone", "email"] | [],
    "ticket_storage": "telegram | google_sheets | ...",
    "documents_urls": ["..."],
    "welcome_message": "...",
    "peak_hours": "..."
  }

КРИТИЧЕСКИ ВАЖНО по секретам:
- Если в ответе клиента встречаются API-токены, пароли, ключи, контакты менеджера (@username, номер телефона) — НЕ копируй их значения в extras. Вместо значения ставь плейсхолдер-флаг (*_placeholder: true или has_*_token: true).
- target_audience/purpose/key_features тоже НЕ должны содержать секретов.
- Если клиент ничего не ответил на вопрос — пропусти соответствующий ключ в extras (не придумывай данные).
- Если клиент не указал язык явно — ставь "ru".
- Если тональность не указана — ставь "friendly"."""


class RequirementsSchema(BaseModel):
    bot_type: Literal["parser", "seller", "content", "support"]
    purpose: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    key_features: list[str] = Field(min_length=1)
    tone: Literal["formal", "friendly", "professional"]
    language: Literal["ru", "en"]
    complexity: Literal["simple", "medium", "complex"]
    extras: dict[str, Any] = Field(default_factory=dict)


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
    user_message = f"Ответы клиента:\n{raw_input}"

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
                f"Ответы клиента:\n{raw_input}"
            )
            continue

        logger.info(
            "analyst_agent: ok (bot_type=%s, features=%d, extras_keys=%d, complexity=%s)",
            validated.bot_type,
            len(validated.key_features),
            len(validated.extras),
            validated.complexity,
        )
        return validated.model_dump()

    raise ValueError(
        f"analyst_agent failed validation after retry: {last_error}"
    ) from last_error
