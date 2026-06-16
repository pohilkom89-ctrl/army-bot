"""Tests for Wave 27: custom/arbitrary bot type."""
from templates.bot_questionnaires import QUESTIONNAIRES


def test_custom_type_exists():
    assert "custom" in QUESTIONNAIRES


def test_custom_has_required_fields():
    spec = QUESTIONNAIRES["custom"]
    assert "name" in spec
    assert "description" in spec
    assert "questions" in spec
    assert "required_integrations" in spec


def test_custom_has_questions():
    questions = QUESTIONNAIRES["custom"]["questions"]
    assert len(questions) >= 5


def test_custom_questions_have_id_text_hint():
    for q in QUESTIONNAIRES["custom"]["questions"]:
        assert "id" in q
        assert "text" in q
        assert "hint" in q


def test_custom_question_ids_sequential():
    questions = QUESTIONNAIRES["custom"]["questions"]
    ids = [q["id"] for q in questions]
    assert ids == list(range(1, len(ids) + 1))


def test_custom_in_bot_type_ru():
    """_BOT_TYPE_RU must have an entry for 'custom'."""
    import importlib, sys
    # Import main module constants without running the bot
    import main as m
    assert "custom" in m._BOT_TYPE_RU


def test_all_questionnaire_types_have_valid_structure():
    """Regression: adding custom must not break existing types."""
    for key, spec in QUESTIONNAIRES.items():
        assert "name" in spec, f"{key}: missing name"
        assert "questions" in spec, f"{key}: missing questions"
        assert len(spec["questions"]) > 0, f"{key}: empty questions"


import pytest


async def test_custom_bot_blocked_for_starter(mocker):
    """Starter tier must not be able to select the 'custom' bot type."""
    alert_texts = []

    mock_cb = mocker.AsyncMock()
    mock_cb.data = "btype:custom"
    mock_cb.from_user = mocker.MagicMock(id=42, username="user42")
    mock_cb.answer = mocker.AsyncMock(side_effect=lambda text="", **kw: alert_texts.append(text))

    mocker.patch("main.get_or_create_client", return_value=mocker.MagicMock(id=1))
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="starter", status="active"),
    )

    from main import on_bot_type_chosen
    from unittest.mock import MagicMock

    state = mocker.AsyncMock()
    await on_bot_type_chosen(mock_cb, state)

    assert any("Pro" in t for t in alert_texts), "Expected Pro upsell message"
    state.update_data.assert_not_called()


async def test_custom_bot_allowed_for_pro(mocker):
    """Pro tier must be able to proceed to questionnaire for 'custom' bot."""
    mock_cb = mocker.AsyncMock()
    mock_cb.data = "btype:custom"
    mock_cb.from_user = mocker.MagicMock(id=42, username="user42")
    mock_cb.message = mocker.AsyncMock()

    mocker.patch("main.get_or_create_client", return_value=mocker.MagicMock(id=1))
    mocker.patch(
        "main.get_active_subscription",
        return_value=mocker.MagicMock(tier="pro", status="active"),
    )
    mock_start = mocker.patch("main._start_questionnaire", new=mocker.AsyncMock())

    from main import on_bot_type_chosen

    state = mocker.AsyncMock()
    await on_bot_type_chosen(mock_cb, state)

    mock_start.assert_called_once()
