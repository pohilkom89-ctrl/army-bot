"""Pipeline-level tests: PII whitelist contract + regenerate_system_prompt
merge logic. These guard the boundary between user-controlled input and
LLM prompts (defense in depth for 152-ФЗ compliance) and the
edit-prompt → runtime sync (tech debt 19 fix)."""


def test_pii_whitelist_includes_required_intake_keys():
    """If these keys go missing, intake breaks. Pinning them prevents
    accidental removal during refactor."""
    from pipeline import _ALLOWED_INPUT_KEYS

    for required in (
        "bot_type",
        "purpose",
        "audience",
        "target_audience",
        "key_features",
        "tone",
    ):
        assert required in _ALLOWED_INPUT_KEYS, (
            f"required intake key {required!r} missing from whitelist"
        )


def test_pii_whitelist_excludes_sensitive_fields():
    """Defense-in-depth for 152-ФЗ: telegram_id / username / phone /
    bot_token must NEVER be passed to LLM. If any of these slips into
    the whitelist, this test fails before the bad input ever ships."""
    from pipeline import _ALLOWED_INPUT_KEYS

    for forbidden in (
        "telegram_id",
        "username",
        "phone",
        "email",
        "bot_token",
        "password",
        "api_key",
    ):
        assert forbidden not in _ALLOWED_INPUT_KEYS, (
            f"sensitive key {forbidden!r} leaked into whitelist — PII risk"
        )


def test_regenerate_system_prompt_merges_style_into_architecture(mocker):
    """communication_style from /mybots → редактировать → стиль must end
    up in the architecture dict that prompt_engineer sees, otherwise the
    style change has no effect on the regenerated prompt."""
    mock_pe = mocker.patch(
        "pipeline.prompt_engineer_agent", return_value="regenerated"
    )

    from pipeline import regenerate_system_prompt

    result = regenerate_system_prompt(
        {
            "architecture": {
                "handlers": [{"command": "/start", "description": "x"}]
            },
            "communication_style": "formal",
            "forbidden_topics": ["politics", "competitors"],
            "scripts": "Use scripts X and Y.",
            "greeting": "Hello!",
        }
    )

    assert result == "regenerated"
    arch_arg = mock_pe.call_args.args[0]
    assert arch_arg["communication_style"] == "formal"
    assert arch_arg["forbidden_topics"] == ["politics", "competitors"]
    assert arch_arg["scripts"] == "Use scripts X and Y."
    assert arch_arg["greeting"] == "Hello!"
    # Original handlers preserved
    assert arch_arg["handlers"] == [
        {"command": "/start", "description": "x"}
    ]


def test_regenerate_system_prompt_skips_empty_edits(mocker):
    """Empty/None/[] values are NOT merged into architecture — otherwise
    a client clearing 'forbidden_topics' would pollute the merged dict
    with [] which the LLM might interpret as 'no topics allowed'."""
    mock_pe = mocker.patch(
        "pipeline.prompt_engineer_agent", return_value="regenerated"
    )

    from pipeline import regenerate_system_prompt

    regenerate_system_prompt(
        {
            "architecture": {"handlers": []},
            "communication_style": "",  # empty
            "forbidden_topics": [],  # empty list
            "scripts": None,  # None
        }
    )

    arch_arg = mock_pe.call_args.args[0]
    assert "communication_style" not in arch_arg
    assert "forbidden_topics" not in arch_arg
    assert "scripts" not in arch_arg
