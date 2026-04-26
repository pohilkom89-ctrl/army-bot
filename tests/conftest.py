"""Pytest configuration shared by every test module.

Sets fake env vars BEFORE any application import. main.py and a few
others raise RuntimeError at module-load time if BOT_TOKEN /
OPENROUTER_API_KEY / similar are missing — without this conftest
the test process couldn't even import the modules under test.
"""
import os

# Must come before any `from pipeline ...` / `from main ...` import below.
os.environ.setdefault("BOT_TOKEN", "1234567890:test-fake-bot-token-xxxxxxxxxxxxxxxxxx")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test-fake-key")
os.environ.setdefault("OPENROUTER_MODEL_AGENTS", "test/agents-model")
os.environ.setdefault("OPENROUTER_MODEL_BOTS", "test/bots-model")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-shared-secret")
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "11111,22222")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEBHOOK_PORT", "8088")

import pytest
import pytest_asyncio


@pytest.fixture
def mock_run_agent(mocker):
    """Mock the single LLM endpoint at pipeline._chat — every agent's
    `run_agent` ultimately delegates to it. Patching agent namespaces
    directly trips the pipeline ↔ agents circular import (broken by
    pipeline's late `from agents.* import …` block); _chat is never
    re-imported into agent namespaces, so this patch reaches everything.

    Tests use it like a normal mock (return_value / side_effect). The
    underlying call signature is _chat(model, system, user_message) —
    user_message is positional arg index 2.
    """
    return mocker.patch("pipeline._chat")


@pytest_asyncio.fixture
async def fresh_db():
    """Create all tables in the configured (in-memory by default) DB and
    return after the test. SQLAlchemy models are dialect-portable so
    SQLite drop-in works the same as Postgres in prod (verified by the
    existing run_e2e.py path)."""
    from db.database import init_db
    await init_db()
    yield
    # in-memory SQLite is dropped on connection close; nothing to clean
