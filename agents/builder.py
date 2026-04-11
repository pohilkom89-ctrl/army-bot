import json
import logging
from typing import Any

from pipeline import run_agent

logger = logging.getLogger(__name__)


BUILDER_SYSTEM_PROMPT = """Ты — senior Python-разработчик, пишущий Telegram-ботов на aiogram 3.x.
На вход — архитектура бота (JSON) и готовый системный промпт для рантайм-обращений к LLM.
Твоя задача — выдать полностью рабочий, готовый к запуску исходник main.py.

Требования к коду:
- Python 3.11, aiogram 3.x (Router/Dispatcher, async/await)
- Реализуй ВСЕ хендлеры из архитектуры (поле handlers)
- Если в архитектуре есть states — используй aiogram.fsm (StatesGroup, FSMContext, MemoryStorage)
- Обращения к LLM через openai SDK поверх OpenRouter:
  * `from openai import AsyncOpenAI`
  * клиент: `AsyncOpenAI(api_key=os.getenv("OPENROUTER_API_KEY"), base_url="https://openrouter.ai/api/v1")`
  * модель берётся из env: `os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")`
  * вызов: `client.chat.completions.create(model=..., max_tokens=2048, messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}])`
  * системный промпт передавай как первое сообщение с role="system"
- Все секреты читай из окружения через os.getenv():
  * BOT_TOKEN
  * OPENROUTER_API_KEY
  * Если ключа нет — падай при старте с понятной ошибкой (RuntimeError)
- Логирование — ТОЛЬКО через loguru: `from loguru import logger`
  * никакого print
  * при старте — logger.info о запуске бота
- Каждый хендлер оборачивай в try/except, ошибки логируй через logger.exception(...)
- Идентификаторы и комментарии — на английском языке
- Точка входа:
    if __name__ == "__main__":
        asyncio.run(main())

Формат ответа:
- Верни ТОЛЬКО исходный код main.py
- Без markdown-блоков, без ```python```, без пояснений до или после
- Никакого текста вне кода"""


def _strip_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def builder_agent(architecture: dict[str, Any], system_prompt: str) -> str:
    logger.info(
        "builder_agent: generating main.py (handlers=%d, storage=%s)",
        len(architecture.get("handlers", [])),
        architecture.get("data_storage"),
    )

    arch_json = json.dumps(architecture, ensure_ascii=False, indent=2)
    user_message = (
        f"Архитектура бота:\n{arch_json}\n\n"
        "Рантайм-промпт, который бот должен передавать в Claude при каждом обращении:\n"
        f"---\n{system_prompt}\n---\n\n"
        "Сгенерируй полностью готовый main.py согласно требованиям системного промпта."
    )

    raw = run_agent(system=BUILDER_SYSTEM_PROMPT, user_message=user_message)
    code = _strip_fence(raw)

    if not code:
        raise ValueError("builder_agent: empty response from LLM")
    if "import" not in code or "def" not in code:
        raise ValueError(
            "builder_agent: response does not look like Python source"
        )

    logger.info("builder_agent: ok (code_len=%d bytes)", len(code))
    return code
