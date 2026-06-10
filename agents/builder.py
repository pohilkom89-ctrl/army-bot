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
- Системный промпт ОБЯЗАТЕЛЬНО читай из файла, НЕ хардкодь литералом:
  * `from pathlib import Path`
  * `SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8").strip()`
  * Файл `system_prompt.txt` уже лежит в /app, deployer кладёт туда актуальный текст из БД при каждом build
  * НЕ копируй текст промпта в исходник — он будет рассинхронизирован после `/mybots → редактировать → промпт`
- Приветствие при /start ОБЯЗАТЕЛЬНО читай из файла:
  * `_greeting_raw = Path("/app/greeting.txt").read_text(encoding="utf-8").strip()`
  * `GREETING = _greeting_raw if _greeting_raw else "Привет! Задайте ваш вопрос."`
  * В хендлере `/start`: `await message.answer(GREETING)`
  * Файл `greeting.txt` уже лежит в /app (может быть пустым — тогда использовать дефолт)
- Чёрный список пользователей читай из файла при старте:
  * `_blacklist_raw = Path("/app/blacklist.txt").read_text(encoding="utf-8")`
  * `BLACKLIST: set[int] = {int(line) for line in _blacklist_raw.splitlines() if line.strip().isdigit()}`
  * В каждом message-хендлере (кроме /start) в самом начале проверяй: `if message.from_user and message.from_user.id in BLACKLIST: return`
  * Файл `blacklist.txt` уже лежит в /app (может быть пустым)
- Webhook для CRM-интеграции:
  * `WEBHOOK_URL = Path("/app/webhook_url.txt").read_text(encoding="utf-8").strip()`
  * Если WEBHOOK_URL не пустой, в каждом message-хендлере (кроме /start) до LLM-вызова делай fire-and-forget: `asyncio.create_task(_fire_webhook(message))`
  * `_fire_webhook` POST'ит JSON с полями: bot_id, telegram_id, username, first_name, text, timestamp (ISO)
  * Используй `aiohttp.ClientSession` с timeout=5с, ловь все исключения через logger.warning (не падай)
  * Файл `webhook_url.txt` уже лежит в /app (может быть пустым — тогда webhook отключён)
- Триггеры по ключевым словам (без LLM):
  * `import json as _json`
  * `TRIGGERS: dict[str, str] = _json.loads(Path("/app/triggers.json").read_text(encoding="utf-8"))`
  * В каждом message-хендлере (кроме /start) ПОСЛЕ проверки blacklist: перебери TRIGGERS, если `keyword.lower() in text.lower()` — ответь response и `return` (не идёт в LLM)
  * Файл `triggers.json` уже лежит в /app (может быть `{}` — тогда триггеры отключены)
- Логирование — ТОЛЬКО через loguru: `from loguru import logger`
  * никакого print
  * при старте — logger.info о запуске бота
- Учёт токенов: ПОСЛЕ каждого вызова `client.chat.completions.create(...)`
  * импортируй `from usage_reporter import report_usage, report_subscriber` (модуль уже лежит в /app, ничего создавать не нужно)
  * сразу после получения response: `asyncio.create_task(report_usage(response.usage, model_used))`
  * вызов fire-and-forget — НЕ await, не блокирует ответ пользователю
  * `model_used` — та же строка что передана в `model=...`, обычно `OPENROUTER_MODEL_BOTS`
  * НЕ пиши свою функцию для этого, НЕ импортируй ничего другого, НЕ делай try/except — usage_reporter сам обрабатывает все сбои
- Учёт подписчиков: в КАЖДОМ хендлере входящего сообщения от пользователя (message handler)
  * добавь одну строку до LLM-вызова: `asyncio.create_task(report_subscriber(message.from_user.id))`
  * import уже включён в строку выше (report_subscriber из usage_reporter)
  * вызов fire-and-forget — НЕ await, не блокирует ответ; все сбои usage_reporter обрабатывает сам
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
