import os
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from loguru import logger

MODEL = "claude-opus-4-5"
MAX_TOKENS = 4096

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


@dataclass
class BotSpec:
    raw_input: str
    requirements: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    bot_code: str = ""


def run_agent(system: str, user_message: str, context: str = "") -> str:
    client = _get_client()
    if context:
        user_message = f"<context>\n{context}\n</context>\n\n{user_message}"

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


# Agent imports live below run_agent to break the circular dependency:
# each agent module imports run_agent from this module.
from agents.analyst import analyst_agent  # noqa: E402
from agents.architect import architect_agent  # noqa: E402
from agents.builder import builder_agent  # noqa: E402
from agents.prompt_engineer import prompt_engineer_agent  # noqa: E402


def run_pipeline(client_answers: dict[str, Any]) -> BotSpec:
    raw_input = str(client_answers)
    spec = BotSpec(raw_input=raw_input)
    total_start = time.perf_counter()

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

    logger.info(
        "pipeline: complete in {:.2f}s total",
        time.perf_counter() - total_start,
    )
    return spec
