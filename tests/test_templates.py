"""Tests for bot_templates: template definitions and STANDARD_BOT_CODE contract."""


def test_all_templates_have_required_fields():
    """Every template has name, emoji, bot_type, description, preview, system_prompt."""
    from bot_templates import TEMPLATES

    required = {"name", "emoji", "bot_type", "description", "preview", "system_prompt"}
    for key, tmpl in TEMPLATES.items():
        missing = required - tmpl.keys()
        assert not missing, f"Template '{key}' missing fields: {missing}"


def test_templates_count():
    """At least 5 templates are defined."""
    from bot_templates import TEMPLATES
    assert len(TEMPLATES) >= 5


def test_get_template_returns_correct():
    """get_template returns the right template for a known key."""
    from bot_templates import get_template
    tmpl = get_template("shop")
    assert tmpl is not None
    assert tmpl["bot_type"] == "seller"


def test_get_template_returns_none_for_unknown():
    """get_template returns None for an unknown key."""
    from bot_templates import get_template
    assert get_template("nonexistent_key_xyz") is None


def test_standard_bot_code_has_required_parts():
    """STANDARD_BOT_CODE contains all critical parts the deployed bot needs."""
    from bot_templates import STANDARD_BOT_CODE

    assert "system_prompt.txt" in STANDARD_BOT_CODE
    assert "report_usage" in STANDARD_BOT_CODE
    assert "report_subscriber" in STANDARD_BOT_CODE
    assert "OPENROUTER_API_KEY" in STANDARD_BOT_CODE
    assert "BOT_TOKEN" in STANDARD_BOT_CODE
    assert "asyncio.run(main())" in STANDARD_BOT_CODE


def test_standard_bot_code_is_valid_python():
    """STANDARD_BOT_CODE compiles without syntax errors."""
    from bot_templates import STANDARD_BOT_CODE
    import ast
    # raises SyntaxError if invalid
    ast.parse(STANDARD_BOT_CODE)


async def test_template_bot_creates_db_row(fresh_db):
    """Creating a bot from template writes a BotConfig row with correct fields."""
    from db.repository import get_or_create_client, save_bot_config, get_client_bots
    from bot_templates import get_template

    client = await get_or_create_client(telegram_id=600, username=None)
    tmpl = get_template("faq")
    assert tmpl is not None

    bot = await save_bot_config(
        client_id=client.id,
        bot_name=f"{tmpl['emoji']} {tmpl['name']}",
        bot_type=tmpl["bot_type"],
        system_prompt=tmpl["system_prompt"],
        config={"model_strategy": "auto", "template_key": "faq"},
        bot_token="tok_tpl_test",
    )
    assert bot.bot_type == "support"
    assert bot.system_prompt == tmpl["system_prompt"]
    bots = await get_client_bots(client.id)
    assert len(bots) == 1
