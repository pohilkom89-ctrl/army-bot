"""Unit tests for the four factory agents + builder template contract.

Each agent is exercised through pipeline.run_agent (mocked) — we don't
hit OpenRouter in tests. We verify schema-level behaviour (valid JSON
parses, invalid retries / fails, optional fields default), not LLM
quality.
"""
import ast
import json

import pytest


# ──── analyst.check_completeness ─────────────────────────────────────


def test_check_completeness_empty_questions(mock_run_agent):
    """When LLM says no clarification needed → empty list."""
    from agents.analyst import check_completeness

    mock_run_agent.return_value = '{"questions": []}'
    result = check_completeness({"1": {"question": "q", "answer": "a"}})
    assert result == []


def test_check_completeness_returns_questions(mock_run_agent):
    """Up to 3 questions are passed through; LLM returning 5 is capped."""
    from agents.analyst import check_completeness

    mock_run_agent.return_value = json.dumps(
        {"questions": ["q1", "q2", "q3", "q4", "q5"]}
    )
    result = check_completeness({"1": {"question": "q", "answer": "a"}})
    assert result == ["q1", "q2", "q3"]
    assert len(result) <= 3


def test_check_completeness_failsafe_on_llm_error(mock_run_agent):
    """LLM exception → returns []. Clarification is enhancement, not gate."""
    from agents.analyst import check_completeness

    mock_run_agent.side_effect = RuntimeError("openrouter down")
    assert check_completeness({"1": {"question": "q", "answer": "a"}}) == []


def test_check_completeness_failsafe_on_invalid_json(mock_run_agent):
    """Malformed JSON → returns []."""
    from agents.analyst import check_completeness

    mock_run_agent.return_value = "this is not json at all"
    assert check_completeness({"1": {"question": "q", "answer": "a"}}) == []


# ──── analyst.analyst_agent ─────────────────────────────────────────


_VALID_REQUIREMENTS = {
    "bot_type": "parser",
    "purpose": "Анализ конкурентов в соцсетях",
    "target_audience": "маркетологи",
    "key_features": ["парсинг", "генерация статей"],
    "tone": "friendly",
    "language": "ru",
    "complexity": "medium",
    "extras": {"sources": ["vk", "telegram"]},
}


def test_analyst_agent_valid_response(mock_run_agent):
    from agents.analyst import analyst_agent

    mock_run_agent.return_value = json.dumps(_VALID_REQUIREMENTS)
    result = analyst_agent("какие-то ответы клиента")

    assert result["bot_type"] == "parser"
    assert result["language"] == "ru"
    assert result["extras"]["sources"] == ["vk", "telegram"]
    # Single LLM call — no retry needed
    assert mock_run_agent.call_count == 1


def test_analyst_agent_retries_on_invalid_json(mock_run_agent):
    """Invalid first response → retries with error feedback → succeeds."""
    from agents.analyst import analyst_agent

    mock_run_agent.side_effect = ["not json", json.dumps(_VALID_REQUIREMENTS)]
    result = analyst_agent("input")

    assert result["bot_type"] == "parser"
    assert mock_run_agent.call_count == 2
    # _chat(model, system, user_message) — user_message is positional [2]
    second_call = mock_run_agent.call_args_list[1]
    second_user_message = second_call.args[2]
    assert "не прошёл валидацию" in second_user_message


def test_analyst_agent_raises_after_two_failures(mock_run_agent):
    from agents.analyst import analyst_agent

    mock_run_agent.return_value = "still not json"
    with pytest.raises(ValueError, match="failed validation"):
        analyst_agent("input")
    assert mock_run_agent.call_count == 2


# ──── analyst bot_type Literal — all 7 types must validate ─────────


def test_analyst_classifies_service_orders(mock_run_agent):
    """service_orders must be a valid bot_type. Before this fix the Literal
    only had 4 base types, so a service_orders intake was force-misclassified
    by the LLM as 'seller' — losing fields the seller-extras schema doesn't
    have (staff, booking_close_hours_before, cancellation_policy, reminders)."""
    from agents.analyst import analyst_agent

    payload = {**_VALID_REQUIREMENTS, "bot_type": "service_orders"}
    mock_run_agent.return_value = json.dumps(payload)
    result = analyst_agent("Барбершоп Lions, мастера, расписание, бронь")

    assert result["bot_type"] == "service_orders"


def test_analyst_classifies_coach(mock_run_agent):
    """coach must be a valid bot_type — same class of bug as service_orders."""
    from agents.analyst import analyst_agent

    payload = {**_VALID_REQUIREMENTS, "bot_type": "coach"}
    mock_run_agent.return_value = json.dumps(payload)
    result = analyst_agent("Фитнес-тренер, программа Похудение за 30 дней, прогресс по замерам")

    assert result["bot_type"] == "coach"


def test_analyst_classifies_creative(mock_run_agent):
    """creative must be a valid bot_type. Distinct from `content`: creative
    produces ideas/concepts/options (brainstorm via Six Hats / SCAMPER /
    naming / pitches), `content` produces finished publishable artifacts."""
    from agents.analyst import analyst_agent

    payload = {**_VALID_REQUIREMENTS, "bot_type": "creative"}
    mock_run_agent.return_value = json.dumps(payload)
    result = analyst_agent(
        "Маркетолог, генерация идей для рекламы, методики Six Hats и SCAMPER, нейминг"
    )

    assert result["bot_type"] == "creative"


def test_analyst_prompt_enumerates_all_seven_types():
    """Schema and the system prompt must agree on which bot_types exist —
    if they drift the LLM either invents a value the validator rejects
    (retry burns tokens) or silently misclassifies into a base type.
    Guards against forgetting to update one side."""
    from agents.analyst import ANALYST_SYSTEM_PROMPT, RequirementsSchema

    schema_types = set(RequirementsSchema.model_fields["bot_type"].annotation.__args__)
    expected = {
        "parser",
        "seller",
        "content",
        "support",
        "service_orders",
        "coach",
        "creative",
    }
    assert schema_types == expected, f"Schema drift: {schema_types ^ expected}"

    for bot_type in expected:
        assert (
            f'"{bot_type}"' in ANALYST_SYSTEM_PROMPT
            or f'- "{bot_type}"' in ANALYST_SYSTEM_PROMPT
        ), f"bot_type {bot_type!r} missing from system prompt"


# ──── architect.architect_agent ──────────────────────────────────────


_VALID_ARCHITECTURE = {
    "handlers": [{"command": "/start", "description": "приветствие"}],
    "states": ["WAITING_INPUT"],
    "external_apis": ["telegram_api"],
    "data_storage": "postgres",
    "scheduled_tasks": [],
    "main_flow": "Пользователь жмёт /start, получает меню, выбирает опцию.",
}


def test_architect_agent_valid_response(mock_run_agent):
    from agents.architect import architect_agent

    mock_run_agent.return_value = json.dumps(_VALID_ARCHITECTURE)
    result = architect_agent(_VALID_REQUIREMENTS)

    assert result["data_storage"] == "postgres"
    assert len(result["handlers"]) == 1
    assert result["handlers"][0]["command"] == "/start"


def test_architect_agent_raises_after_failures(mock_run_agent):
    from agents.architect import architect_agent

    mock_run_agent.return_value = '{"missing": "fields"}'
    with pytest.raises(ValueError, match="failed validation"):
        architect_agent(_VALID_REQUIREMENTS)


# ──── prompt_engineer.prompt_engineer_agent ─────────────────────────


def test_prompt_engineer_returns_text(mock_run_agent):
    from agents.prompt_engineer import prompt_engineer_agent

    mock_run_agent.return_value = "Ты — SEO-ассистент для анализа..."
    result = prompt_engineer_agent(_VALID_ARCHITECTURE)

    assert result.startswith("Ты — SEO-ассистент")


def test_prompt_engineer_strips_markdown_fences(mock_run_agent):
    from agents.prompt_engineer import prompt_engineer_agent

    mock_run_agent.return_value = "```\nТы — бот\n```"
    result = prompt_engineer_agent(_VALID_ARCHITECTURE)

    assert "```" not in result
    assert "Ты — бот" in result


# ──── builder.builder_agent ─────────────────────────────────────────


_VALID_BOT_CODE = """\
import asyncio
from aiogram import Bot, Dispatcher

async def main():
    pass

if __name__ == "__main__":
    asyncio.run(main())
"""


def test_builder_returns_valid_python(mock_run_agent):
    from agents.builder import builder_agent

    mock_run_agent.return_value = _VALID_BOT_CODE
    code = builder_agent(_VALID_ARCHITECTURE, "Ты — бот")

    # Sanity: actually parses as Python
    ast.parse(code)
    assert "import" in code and "def" in code


def test_builder_strips_markdown_fence(mock_run_agent):
    from agents.builder import builder_agent

    mock_run_agent.return_value = f"```python\n{_VALID_BOT_CODE}```"
    code = builder_agent(_VALID_ARCHITECTURE, "Ты — бот")

    assert "```" not in code
    ast.parse(code)


def test_builder_rejects_empty_response(mock_run_agent):
    from agents.builder import builder_agent

    mock_run_agent.return_value = ""
    with pytest.raises(ValueError, match="empty response"):
        builder_agent(_VALID_ARCHITECTURE, "Ты — бот")


def test_builder_rejects_non_python_response(mock_run_agent):
    """Sanity check: code must contain `import` and `def`."""
    from agents.builder import builder_agent

    mock_run_agent.return_value = "Конечно! Вот ваш бот:\nprint('hi')"
    with pytest.raises(ValueError, match="does not look like Python"):
        builder_agent(_VALID_ARCHITECTURE, "Ты — бот")


# ──── BUILDER_SYSTEM_PROMPT contract (tech debt 19) ─────────────────


def test_builder_template_requires_system_prompt_from_file():
    """Tech debt 19 fix: every generated bot must read SYSTEM_PROMPT from
    /app/system_prompt.txt, not hardcode the text. The instruction must
    stay in the builder template otherwise the LLM regresses to inlining
    the literal and edits via /mybots → prompt silently break."""
    from agents.builder import BUILDER_SYSTEM_PROMPT

    assert "/app/system_prompt.txt" in BUILDER_SYSTEM_PROMPT
    assert "Path" in BUILDER_SYSTEM_PROMPT  # mentions pathlib.Path
    # Explicit "do not hardcode" guardrail
    assert (
        "НЕ хардкодь" in BUILDER_SYSTEM_PROMPT
        or "не хардкодь" in BUILDER_SYSTEM_PROMPT.lower()
    )


def test_builder_template_requires_usage_reporter():
    """Tech debt closed earlier (runtime token accounting): every generated
    bot must call report_usage after each LLM call. If this regresses,
    client bots burn tokens without billing."""
    from agents.builder import BUILDER_SYSTEM_PROMPT

    assert "usage_reporter" in BUILDER_SYSTEM_PROMPT
    assert "report_usage" in BUILDER_SYSTEM_PROMPT
    assert "create_task" in BUILDER_SYSTEM_PROMPT  # fire-and-forget pattern
