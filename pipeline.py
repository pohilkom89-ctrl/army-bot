import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from openai import OpenAI

from config import MODELS
from settings import settings

OPENROUTER_BASE_URL = settings.openrouter_base_url
MODEL_AGENTS = settings.openrouter_model_agents
# Legacy default when a caller of run_bot_query doesn't pass tier — points
# at the balanced tier so behaviour stays the same for callers that didn't
# opt into multi-LLM routing.
MODEL_BOTS = settings.openrouter_model_bots
MAX_TOKENS = 4096

# Whitelist of fields allowed into LLM prompts. Anything else (telegram_id,
# username, phone, etc.) must never reach the model — 152-ФЗ minimization.
_ALLOWED_INPUT_KEYS = frozenset(
    {
        "bot_type",
        "purpose",
        "audience",
        "target_audience",
        "key_features",
        "tone",
        "questionnaire_type",
        "answers",
        "clarification_answers",
    }
)

# Per-pipeline token usage accumulator. run_pipeline sets a fresh list in this
# ContextVar; run_agent/run_bot_query append one dict per LLM call. main.py
# reads spec.token_logs after the pipeline finishes and writes them to the DB.
_token_accumulator: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "token_accumulator", default=None
)


def _record_usage(model: str, response: Any) -> None:
    acc = _token_accumulator.get()
    if acc is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    acc.append(
        {
            "model": model,
            "tokens_in": int(getattr(usage, "prompt_tokens", 0) or 0),
            "tokens_out": int(getattr(usage, "completion_tokens", 0) or 0),
        }
    )


_client: OpenAI | None = None
_yandex_client: OpenAI | None = None

_YANDEX_BASE_URL = "https://llm.api.cloud.yandex.net/v1"


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY env var is required")
        _client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    return _client


def _get_yandex_client() -> OpenAI:
    global _yandex_client
    if _yandex_client is None:
        api_key = settings.yandex_api_key
        folder_id = settings.yandex_folder_id
        if not api_key or not folder_id:
            raise RuntimeError("YANDEX_API_KEY and YANDEX_FOLDER_ID are required")
        _yandex_client = OpenAI(
            api_key=api_key,
            base_url=_YANDEX_BASE_URL,
            default_headers={"x-folder-id": folder_id},
        )
    return _yandex_client


@dataclass
class BotSpec:
    raw_input: str
    requirements: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    bot_code: str = ""
    token_logs: list[dict[str, Any]] = field(default_factory=list)


def _chat(model: str, system: str, user_message: str) -> str:
    # "yandex:<slug>" → Yandex AI Studio client; everything else → OpenRouter.
    if model.startswith("yandex:"):
        client = _get_yandex_client()
        real_model = model[len("yandex:"):]
    else:
        client = _get_client()
        real_model = model
    response = client.chat.completions.create(
        model=real_model,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
    )
    _record_usage(real_model, response)
    content = response.choices[0].message.content or ""
    return content.strip()


def run_agent(system: str, user_message: str, context: str = "") -> str:
    """LLM call for factory agents (analyst/architect/prompt_engineer/builder)."""
    if context:
        user_message = f"<context>\n{context}\n</context>\n\n{user_message}"
    return _chat(MODEL_AGENTS, system, user_message)


def run_bot_query(
    system: str,
    user_message: str,
    context: str = "",
    tier: str = "balanced",
) -> str:
    """LLM call for generated runtime bots. `tier` selects a model from
    config.MODELS ('cheap' | 'balanced' | 'smart'); unknown tiers fall
    back to the legacy `MODEL_BOTS` env var to keep behaviour stable."""
    if context:
        user_message = f"<context>\n{context}\n</context>\n\n{user_message}"
    model = MODELS.get(tier, MODEL_BOTS)
    return _chat(model, system, user_message)


def run_with_model(model: str, system: str, user_message: str) -> str:
    """Direct-model LLM call, used by agents (e.g. the router) that need
    to pin a specific slug regardless of tier strategy. Goes through the
    same _chat path so token accounting is unchanged."""
    return _chat(model, system, user_message)


# Agent imports live below run_agent to break the circular dependency:
# each agent module imports run_agent from this module.
from agents.analyst import analyst_agent  # noqa: E402
from agents.architect import architect_agent  # noqa: E402
from agents.builder import builder_agent  # noqa: E402
from agents.prompt_engineer import prompt_engineer_agent  # noqa: E402


# TODO(integrations): consume QUESTIONNAIRES[qtype]["required_integrations"]
#   - select architecture template variant (e.g. add calendar/payments scaffold
#     when integrations include calendar_logic / payment_optional)
#   - warn the client during intake if any required integration needs an
#     external API key they must provide (e.g. vk_api, google_sheets)
def _format_raw_input(safe_answers: dict[str, Any]) -> str:
    """Build a human-readable block for the analyst LLM prompt.

    When the intake flow delivers a `answers` dict (questionnaire-based),
    render it as a numbered Q&A list so the analyst can reason over each
    answer individually instead of parsing a Python repr.
    """
    lines: list[str] = []
    qtype = safe_answers.get("questionnaire_type") or safe_answers.get("bot_type")
    if qtype:
        lines.append(f"Тип бота (выбран клиентом): {qtype}")

    answers = safe_answers.get("answers")
    if isinstance(answers, dict) and answers:
        lines.append("\nОтветы клиента на анкету:")
        for qid in sorted(answers.keys(), key=lambda k: int(k) if str(k).isdigit() else 0):
            entry = answers[qid]
            if isinstance(entry, dict):
                q = entry.get("question", "")
                a = entry.get("answer", "")
                lines.append(f"Q{qid}: {q}\nA{qid}: {a}")
            else:
                lines.append(f"Q{qid}: {entry}")

        clarifications = safe_answers.get("clarification_answers")
        if isinstance(clarifications, dict) and clarifications:
            lines.append("\nУточняющие вопросы и ответы:")
            for cid in sorted(
                clarifications.keys(),
                key=lambda k: int(k) if str(k).isdigit() else 0,
            ):
                entry = clarifications[cid]
                if isinstance(entry, dict):
                    q = entry.get("question", "")
                    a = entry.get("answer", "")
                    lines.append(f"Q: {q}\nA: {a}")
                else:
                    lines.append(f"- {entry}")
        return "\n".join(lines)

    for key in ("purpose", "audience", "target_audience", "key_features", "tone"):
        if key in safe_answers and safe_answers[key]:
            lines.append(f"{key}: {safe_answers[key]}")
    return "\n".join(lines) if lines else str(safe_answers)


def run_pipeline(client_answers: dict[str, Any]) -> BotSpec:
    safe_answers = {
        k: v for k, v in client_answers.items() if k in _ALLOWED_INPUT_KEYS
    }
    dropped = set(client_answers) - _ALLOWED_INPUT_KEYS
    if dropped:
        logger.warning(
            "pipeline: dropped non-whitelisted input keys: {}", sorted(dropped)
        )

    raw_input = _format_raw_input(safe_answers)
    spec = BotSpec(raw_input=raw_input)
    total_start = time.perf_counter()

    token_logs: list[dict[str, Any]] = []
    token_ctx = _token_accumulator.set(token_logs)

    logger.info("pipeline: start (raw_input_len={} chars)", len(raw_input))

    t0 = time.perf_counter()
    logger.info("pipeline[1/4]: analyst started")
    spec.requirements = analyst_agent(raw_input)
    logger.info(
        "pipeline[1/4]: analyst done in {:.2f}s (bot_type={})",
        time.perf_counter() - t0,
        spec.requirements.get("bot_type"),
    )

    t0 = time.perf_counter()
    logger.info("pipeline[2/4]: architect started")
    spec.architecture = architect_agent(spec.requirements)
    logger.info(
        "pipeline[2/4]: architect done in {:.2f}s (handlers={}, storage={})",
        time.perf_counter() - t0,
        len(spec.architecture.get("handlers", [])),
        spec.architecture.get("data_storage"),
    )

    t0 = time.perf_counter()
    logger.info("pipeline[3/4]: prompt_engineer started")
    spec.system_prompt = prompt_engineer_agent(spec.architecture)
    logger.info(
        "pipeline[3/4]: prompt_engineer done in {:.2f}s (prompt_len={} chars)",
        time.perf_counter() - t0,
        len(spec.system_prompt),
    )

    t0 = time.perf_counter()
    logger.info("pipeline[4/4]: builder started")
    spec.bot_code = builder_agent(spec.architecture, spec.system_prompt)
    logger.info(
        "pipeline[4/4]: builder done in {:.2f}s (code_len={} bytes)",
        time.perf_counter() - t0,
        len(spec.bot_code),
    )

    spec.token_logs = list(token_logs)
    _token_accumulator.reset(token_ctx)

    total_in = sum(e["tokens_in"] for e in spec.token_logs)
    total_out = sum(e["tokens_out"] for e in spec.token_logs)
    logger.info(
        "pipeline: complete in {:.2f}s total (tokens_in={}, tokens_out={}, calls={})",
        time.perf_counter() - total_start,
        total_in,
        total_out,
        len(spec.token_logs),
    )
    return spec


_EDIT_KEYS_FOR_REGEN = (
    "communication_style",
    "forbidden_topics",
    "scripts",
    "greeting",
)


def regenerate_system_prompt(bot_config: dict[str, Any]) -> str:
    """Rebuild system_prompt from a BotConfig.config_json dict, merging
    client-side edits (style/forbidden/scripts/greeting) into the original
    architecture before handing it to the prompt-engineer LLM. The caller
    is responsible for persisting the returned string via
    repository.update_bot_system_prompt.
    """
    architecture = dict(bot_config.get("architecture") or {})
    for key in _EDIT_KEYS_FOR_REGEN:
        if key in bot_config and bot_config[key] not in (None, "", [], {}):
            architecture[key] = bot_config[key]
    return prompt_engineer_agent(architecture)


def merge_bots_prompt(name: str, bots: list[Any]) -> str:
    """Generate a unified system prompt for a merged bot.

    Combines the architectures of all source bots into a single merged
    architecture and runs it through prompt_engineer_agent to produce
    a coherent multi-role system prompt.
    """
    sources = []
    combined_handlers: list[Any] = []
    for bot in bots:
        cfg = bot.config_json or {}
        arch = cfg.get("architecture") or {}
        handlers = arch.get("handlers", [])
        combined_handlers.extend(handlers)
        sources.append({
            "name": bot.bot_name,
            "type": bot.bot_type,
            "purpose": arch.get("purpose") or cfg.get("purpose", ""),
            "main_flow": arch.get("main_flow", ""),
        })

    merged_arch = {
        "bot_name": name,
        "bot_type": "merged",
        "purpose": (
            f"Многофункциональный бот «{name}», объединяющий "
            f"{len(sources)} специализации: "
            + ", ".join(s["name"] for s in sources)
        ),
        "merged_from": sources,
        "handlers": combined_handlers,
        "data_storage": "postgresql",
        "note": (
            "Определяй намерение пользователя и помогай в рамках всех "
            "доступных специализаций бота."
        ),
    }
    return prompt_engineer_agent(merged_arch)
