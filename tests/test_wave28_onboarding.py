"""Tests for Wave 28: onboarding improvements."""
import pytest


def test_welcome_text_constant_exists():
    from main import WELCOME_TEXT
    assert "ArmyBots" in WELCOME_TEXT
    assert "бота" in WELCOME_TEXT
    assert len(WELCOME_TEXT) > 50


def test_welcome_text_mentions_consent():
    from main import WELCOME_TEXT
    assert "согласие" in WELCOME_TEXT.lower() or "данных" in WELCOME_TEXT.lower()


def test_format_question_has_progress_bar():
    from main import _format_question
    result = _format_question({"text": "Вопрос", "hint": ""}, 1, 5)
    assert "■" in result or "□" in result
    assert "[" in result and "]" in result


def test_format_question_bar_full_at_end():
    from main import _format_question
    result = _format_question({"text": "Последний вопрос", "hint": ""}, 5, 5)
    assert "■■■■■" in result
    assert "□" not in result


def test_format_question_bar_mostly_empty_at_start():
    from main import _format_question
    result = _format_question({"text": "Первый вопрос", "hint": ""}, 1, 10)
    assert result.count("■") < result.count("□") + 2


def test_all_questionnaires_have_example():
    from templates.bot_questionnaires import QUESTIONNAIRES
    for key, spec in QUESTIONNAIRES.items():
        assert "example" in spec, f"Missing 'example' in QUESTIONNAIRES['{key}']"
        assert len(spec["example"]) > 10, f"Empty example in QUESTIONNAIRES['{key}']"


def test_post_create_next_steps_has_commands():
    from main import _post_create_next_steps
    pro_text = _post_create_next_steps("pro")
    assert "/teach" in pro_text
    assert "/chat" in pro_text
    assert "/broadcast" in pro_text
    starter_text = _post_create_next_steps("starter")
    assert "/teach" not in starter_text
    assert "/chat" in starter_text
    assert "/subscribe" in starter_text
