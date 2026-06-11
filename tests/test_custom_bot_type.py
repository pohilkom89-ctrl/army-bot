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
