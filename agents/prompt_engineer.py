import json
import logging
from typing import Any

from pipeline import run_agent

logger = logging.getLogger(__name__)


PROMPT_ENGINEER_SYSTEM_PROMPT = """Ты — prompt-инженер, пишущий системные промпты для LLM-driven Telegram-ботов.
На вход — архитектура будущего бота (JSON): handlers, states, main_flow, external_apis, data_storage и другие поля.

Твоя задача — написать готовый системный промпт, который этот бот будет использовать при обращении к LLM в рантайме.

Требования к промпту:
- Обращайся к модели на "ты", начинай со слов "Ты — ...".
- Чётко опиши роль бота, его цель и целевую аудиторию (исходя из main_flow и handlers).
- Задай тон общения (дружелюбный / формальный / профессиональный) — выбери подходящий по контексту архитектуры; если явных указаний нет — дружелюбный профессиональный.
- Перечисли, ЧТО бот должен делать и ЧЕГО НЕ должен (границы ответственности).
- Если есть FSM-состояния — объясни, как вести пользователя по сценарию.
- Если есть внешние API — опиши, когда их вызывать.
- Отвечай пользователю на том же языке, на котором он пишет (по умолчанию — русский).
- Длина промпта — 10-30 строк, без воды.

Формат ответа:
- Только готовый текст промпта. Без заголовков, без комментариев, без markdown-блоков, без кода-фенсов, без пояснений до или после."""


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def prompt_engineer_agent(architecture: dict[str, Any]) -> str:
    logger.info(
        "prompt_engineer_agent: building prompt (handlers=%d, storage=%s)",
        len(architecture.get("handlers", [])),
        architecture.get("data_storage"),
    )

    arch_json = json.dumps(architecture, ensure_ascii=False, indent=2)
    raw = run_agent(
        system=PROMPT_ENGINEER_SYSTEM_PROMPT,
        user_message=f"Архитектура бота:\n{arch_json}",
    )

    prompt = _strip_fence(raw)
    if not prompt:
        raise ValueError("prompt_engineer_agent: empty response from LLM")

    logger.info("prompt_engineer_agent: ok (prompt_len=%d chars)", len(prompt))
    return prompt
