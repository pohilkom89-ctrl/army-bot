import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pipeline import run_agent

logger = logging.getLogger(__name__)


ARCHITECT_SYSTEM_PROMPT = """Ты — software-архитектор Telegram-ботов на aiogram 3.x.
На вход — структурированные требования от аналитика (JSON).
Твоя задача — спроектировать техническую структуру бота.

Верни СТРОГО валидный JSON без markdown-блоков, без пояснений, без кода-фенсов.

Схема ответа:
{
  "handlers": [
    {"command": "/start", "description": "что делает хендлер"}
  ],
  "states": ["WAITING_INPUT", "PROCESSING"],
  "external_apis": ["api_name_1", "api_name_2"],
  "data_storage": "none" | "redis" | "postgres",
  "scheduled_tasks": [
    {"name": "daily_report", "cron": "0 9 * * *", "description": "..."}
  ],
  "main_flow": "описание главного пользовательского сценария (1-3 предложения)"
}

Правила проектирования:
- handlers — минимум /start; добавь /help и остальные команды из требований.
- states — используй FSM-состояния aiogram, только если нужен многошаговый диалог; иначе верни пустой список [].
- external_apis — перечисли внешние сервисы, к которым бот должен обращаться.
- data_storage:
  * "none"     — бот не хранит состояние между сессиями
  * "redis"    — нужен быстрый кеш/FSM/короткоживущие данные
  * "postgres" — нужны персистентные структурированные данные
- scheduled_tasks — только если в требованиях явно указаны регулярные действия; иначе [].
- main_flow — опиши путь пользователя от первого сообщения до результата."""


class HandlerSpec(BaseModel):
    command: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ScheduledTaskSpec(BaseModel):
    name: str = Field(min_length=1)
    cron: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ArchitectureSchema(BaseModel):
    handlers: list[HandlerSpec] = Field(min_length=1)
    states: list[str]
    external_apis: list[str]
    data_storage: Literal["none", "redis", "postgres"]
    scheduled_tasks: list[ScheduledTaskSpec]
    main_flow: str = Field(min_length=1)


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def architect_agent(requirements: dict[str, Any]) -> dict[str, Any]:
    logger.info(
        "architect_agent: designing for bot_type=%s",
        requirements.get("bot_type"),
    )
    req_json = json.dumps(requirements, ensure_ascii=False, indent=2)
    user_message = f"Требования от аналитика:\n{req_json}"

    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = run_agent(system=ARCHITECT_SYSTEM_PROMPT, user_message=user_message)
        try:
            parsed = json.loads(_strip_fence(raw))
            validated = ArchitectureSchema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as err:
            last_error = err
            logger.warning(
                "architect_agent: invalid output on attempt %d: %s", attempt, err
            )
            user_message = (
                f"Твой предыдущий ответ не прошёл валидацию: {err}\n"
                "Верни СТРОГО валидный JSON по схеме, без markdown и пояснений.\n\n"
                f"Требования от аналитика:\n{req_json}"
            )
            continue

        logger.info(
            "architect_agent: ok (handlers=%d, states=%d, storage=%s)",
            len(validated.handlers),
            len(validated.states),
            validated.data_storage,
        )
        return validated.model_dump()

    raise ValueError(
        f"architect_agent failed validation after retry: {last_error}"
    ) from last_error
