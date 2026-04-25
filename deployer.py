"""Docker orchestrator for generated client bots.

Each BotConfig in the database maps to a single Docker container named
`bot_client_{bot_id}`. Container name uses bot_id (PK) not client_id so
tier=pro/business clients (3–10 bots) don't collide. The factory itself
runs on the host through systemd; only generated client bots live in
containers.

All high-level calls (deploy_bot / stop_bot / remove_bot / get_bot_status
/ get_bot_logs) are async and wrap the sync python-on-whales API via
asyncio.to_thread. Callers do not block the event loop.
"""

import asyncio
import os
import shutil
from pathlib import Path

from loguru import logger
from python_on_whales import docker

from config import CONTAINER_CPU_LIMIT, CONTAINER_MEMORY_LIMIT
from db.repository import get_bot_by_id_any

BOTS_DIR = Path("bots")
RUNTIME_DIR = Path("bot_runtime")

# Runtime image for generated bots — superset of packages the builder agent
# might emit code against (aiogram/openai + HTTP/DB/image/HTML tooling).
# If a generated bot imports something not listed, the container will fail
# on first run and get_bot_status will report "error" — a signal we need
# to extend this list.
DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \\
    aiogram==3.13.0 \\
    openai==2.31.0 \\
    loguru==0.7.3 \\
    python-dotenv==1.0.1 \\
    aiohttp==3.10.11 \\
    pypdf==5.1.0 \\
    requests==2.32.3 \\
    asyncpg==0.30.0 \\
    "sqlalchemy[asyncio]==2.0.36" \\
    redis==5.2.1 \\
    pillow==11.0.0 \\
    beautifulsoup4==4.12.3

COPY main.py /app/main.py
COPY usage_reporter.py /app/usage_reporter.py
COPY system_prompt.txt /app/system_prompt.txt

CMD ["python", "main.py"]
"""


def _bot_dir(bot_id: int) -> Path:
    return BOTS_DIR / str(bot_id)


def _container_name(bot_id: int) -> str:
    return f"bot_client_{bot_id}"


def _image_tag(bot_id: int) -> str:
    return f"bot_factory/bot_client_{bot_id}:latest"


def prepare_bot_files(bot_code: str, bot_id: int) -> Path:
    bot_dir = _bot_dir(bot_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text(bot_code, encoding="utf-8")
    _write_dockerfile(bot_id)
    _ensure_runtime_files(bot_dir)
    logger.info("deployer: prepared files for bot_id={}", bot_id)
    return bot_dir


def _write_dockerfile(bot_id: int) -> Path:
    bot_dir = _bot_dir(bot_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = bot_dir / "Dockerfile"
    dockerfile.write_text(DOCKERFILE_TEMPLATE, encoding="utf-8")
    logger.info("deployer: Dockerfile written for bot_id={}", bot_id)
    return dockerfile


def _ensure_runtime_files(bot_dir: Path) -> None:
    """Copy shared runtime helpers into the bot's Docker context. Always
    overwrites so updates to the helper propagate on the next rebuild."""
    src = RUNTIME_DIR / "usage_reporter.py"
    if not src.exists():
        raise FileNotFoundError(
            f"deployer: runtime helper {src} missing — cannot build bot images"
        )
    shutil.copy2(src, bot_dir / "usage_reporter.py")


def _write_system_prompt(bot_dir: Path, system_prompt: str) -> None:
    """Persist the bot's current system_prompt to a file in the build
    context. The container reads this at startup — keeps the runtime
    in sync with /mybots → edit prompt without hardcoding the text in
    main.py (closes tech debt 19)."""
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "system_prompt.txt").write_text(
        system_prompt or "", encoding="utf-8"
    )


def build_bot_image(bot_dir: Path, bot_id: int) -> str:
    """Build the runtime image for this bot. Returns the tag. Sync — call
    via asyncio.to_thread from async code."""
    tag = _image_tag(bot_id)
    if not (bot_dir / "main.py").exists():
        raise FileNotFoundError(
            f"deployer: {bot_dir / 'main.py'} not found — "
            "call prepare_bot_files first"
        )
    # Always refresh Dockerfile + runtime helpers so rebuilds pick up
    # template changes without needing to re-run prepare_bot_files.
    _write_dockerfile(bot_id)
    _ensure_runtime_files(bot_dir)
    logger.info("deployer: building image {}", tag)
    docker.build(context_path=str(bot_dir), tags=[tag])
    return tag


async def deploy_bot(bot_id: int) -> str:
    """Full deploy pipeline: prepare → build → run (or start if stopped).
    Idempotent — safe to call on a bot that is already running. If the
    container dies within 3s with a Telegram 409 in its logs, raises
    RuntimeError with a clear message about polling conflict."""
    bot = await get_bot_by_id_any(bot_id)
    if bot is None:
        raise RuntimeError(f"deployer: no BotConfig for bot_id={bot_id}")
    bot_token = bot.bot_token
    if not bot_token:
        raise RuntimeError(
            f"deployer: bot_token is empty for bot_id={bot_id}"
        )

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key:
        raise RuntimeError("OPENROUTER_API_KEY is required to deploy bots")
    model_bots = os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")
    internal_api_key = os.getenv("INTERNAL_API_KEY")
    if not internal_api_key:
        raise RuntimeError("INTERNAL_API_KEY is required to deploy bots")
    factory_url = os.getenv("FACTORY_URL", "http://host.docker.internal:8080")
    system_prompt = bot.system_prompt or ""

    container_id = await asyncio.to_thread(
        _deploy_sync,
        bot_id,
        bot_token,
        openrouter_key,
        model_bots,
        factory_url,
        internal_api_key,
        system_prompt,
    )

    # Give the container a moment to make its first Telegram poll, then
    # check for the tell-tale 409 Conflict (another process on this token).
    await asyncio.sleep(3)
    status = await get_bot_status(bot_id)
    if status != "running":
        logs_tail = await get_bot_logs(bot_id, lines=100)
        if (
            "Conflict: terminated by other" in logs_tail
            or "TelegramConflictError" in logs_tail
            or "409" in logs_tail
        ):
            logger.error(
                "deployer: Telegram polling conflict: "
                "другой процесс держит тот же токен (bot_id={})",
                bot_id,
            )
            raise RuntimeError(
                "Telegram polling conflict: another process holds this bot token"
            )
        logger.warning(
            "deployer: container bot_client_{} not running after deploy "
            "(status={!r}). Check logs via get_bot_logs.",
            bot_id,
            status,
        )

    return container_id


def _deploy_sync(
    bot_id: int,
    bot_token: str,
    openrouter_key: str,
    model_bots: str,
    factory_url: str,
    internal_api_key: str,
    system_prompt: str,
) -> str:
    bot_dir = _bot_dir(bot_id)
    name = _container_name(bot_id)

    # If a container exists and is merely stopped, starting it is ~instant
    # vs ~20-60s for a full rebuild. Running → noop.
    if docker.container.exists(name):
        state = _container_state(name)
        if state == "running":
            logger.info("deployer: {} already running — noop", name)
            return _container_id(name)
        if state in ("exited", "created"):
            logger.info("deployer: starting existing stopped container {}", name)
            docker.container.start(name)
            return _container_id(name)
        logger.info(
            "deployer: {} in unexpected state {!r} — removing for rebuild",
            name,
            state,
        )
        docker.container.remove(name, force=True)

    if not (bot_dir / "main.py").exists():
        raise FileNotFoundError(
            f"deployer: {bot_dir / 'main.py'} not found — "
            "call prepare_bot_files first"
        )
    _write_system_prompt(bot_dir, system_prompt)
    build_bot_image(bot_dir, bot_id)
    tag = _image_tag(bot_id)

    logger.info("deployer: starting container {}", name)
    container = docker.run(
        image=tag,
        name=name,
        detach=True,
        restart="unless-stopped",
        cpus=CONTAINER_CPU_LIMIT,
        memory=CONTAINER_MEMORY_LIMIT,
        # On Linux host.docker.internal isn't auto-resolvable like on
        # Docker Desktop. host-gateway makes the bridge gateway IP
        # available so containers can reach the factory's webhook server.
        add_hosts=[("host.docker.internal", "host-gateway")],
        envs={
            "BOT_TOKEN": bot_token,
            "BOT_ID": str(bot_id),
            "OPENROUTER_API_KEY": openrouter_key,
            "OPENROUTER_MODEL_BOTS": model_bots,
            "FACTORY_URL": factory_url,
            "INTERNAL_API_KEY": internal_api_key,
        },
    )
    container_id = getattr(container, "id", str(container))
    logger.info("deployer: container {} started (id={})", name, container_id)
    return container_id


def _container_id(name: str) -> str:
    info = docker.container.inspect(name)
    return getattr(info, "id", "") or ""


def _container_state(name: str) -> str:
    info = docker.container.inspect(name)
    state = getattr(info, "state", None)
    return (getattr(state, "status", "") or "").lower()


async def stop_bot(bot_id: int) -> None:
    """Stop the container but leave it on disk so deploy_bot can fast-start
    it on resume. Noop if the container does not exist."""
    await asyncio.to_thread(_stop_sync, bot_id)


async def redeploy_bot(bot_id: int) -> str:
    """Force a fresh build + restart for an existing bot. Used after edits
    to system_prompt / config (where the build context changes but main.py
    structure does not). Skips the deploy if the bot is paused — the prompt
    on disk will be picked up on next resume.

    Unlike remove_bot, this does NOT wipe bots/{bot_id}/ — only the running
    container is removed so deploy_bot rebuilds the image from current files.
    Returns 'paused-skipped' when the bot is paused, otherwise the new
    container id.
    """
    bot = await get_bot_by_id_any(bot_id)
    if bot is None:
        raise RuntimeError(f"redeploy_bot: no BotConfig for bot_id={bot_id}")
    if (getattr(bot, "status", None) or "").lower() == "paused":
        logger.info(
            "redeploy_bot: bot_id={} paused — config will apply on resume",
            bot_id,
        )
        return "paused-skipped"
    await asyncio.to_thread(_remove_container_only_sync, bot_id)
    return await deploy_bot(bot_id)


def _remove_container_only_sync(bot_id: int) -> None:
    """Drop the running container so deploy_bot takes the rebuild branch.
    Image and bot_dir stay intact; only the container instance is removed."""
    name = _container_name(bot_id)
    if not docker.container.exists(name):
        return
    state = _container_state(name)
    if state == "running":
        try:
            docker.container.stop(name, time=10)
        except Exception:
            logger.exception("deployer: stop before remove failed for {}", name)
    try:
        docker.container.remove(name, force=True)
        logger.info("deployer: container {} removed (image kept)", name)
    except Exception:
        logger.exception("deployer: container remove failed for {}", name)


def _stop_sync(bot_id: int) -> None:
    name = _container_name(bot_id)
    if not docker.container.exists(name):
        logger.info("deployer: {} not found, stop is a noop", name)
        return
    state = _container_state(name)
    if state != "running":
        logger.info("deployer: {} already in state {!r}, noop", name, state)
        return
    docker.container.stop(name)
    logger.info("deployer: container {} stopped", name)


async def remove_bot(bot_id: int) -> None:
    """Hard-delete: stop + remove container + remove image + wipe bot_dir.
    Runs docker image prune after to reap dangling layers. Noop-safe."""
    await asyncio.to_thread(_remove_sync, bot_id)


def _remove_sync(bot_id: int) -> None:
    name = _container_name(bot_id)
    tag = _image_tag(bot_id)
    bot_dir = _bot_dir(bot_id)

    if docker.container.exists(name):
        try:
            docker.container.remove(name, force=True)
            logger.info("deployer: removed container {}", name)
        except Exception:
            logger.exception("deployer: failed to remove container {}", name)

    try:
        if docker.image.exists(tag):
            docker.image.remove(tag, force=True)
            logger.info("deployer: removed image {}", tag)
    except Exception:
        logger.exception("deployer: failed to remove image {}", tag)

    if bot_dir.exists():
        try:
            shutil.rmtree(bot_dir)
            logger.info("deployer: wiped {}", bot_dir)
        except Exception:
            logger.exception("deployer: failed to wipe {}", bot_dir)

    try:
        docker.image.prune(all=False, filters={"dangling": "true"})
    except Exception:
        # Prune is nice-to-have; don't fail the whole remove if it errors.
        logger.exception("deployer: image prune failed")


async def get_bot_status(bot_id: int) -> str:
    """Return 'running' | 'stopped' | 'not_deployed' | 'error'. Never raises."""
    return await asyncio.to_thread(_status_sync, bot_id)


def _status_sync(bot_id: int) -> str:
    name = _container_name(bot_id)
    try:
        if not docker.container.exists(name):
            return "not_deployed"
        state = _container_state(name)
        if state == "running":
            return "running"
        if state in ("exited", "created", "paused"):
            return "stopped"
        return "error"
    except Exception:
        logger.exception("deployer: status check failed for bot_id={}", bot_id)
        return "error"


async def get_bot_logs(bot_id: int, lines: int = 50) -> str:
    """Last N lines of container stdout/stderr. Empty string on error."""
    return await asyncio.to_thread(_logs_sync, bot_id, lines)


def _logs_sync(bot_id: int, lines: int) -> str:
    name = _container_name(bot_id)
    try:
        if not docker.container.exists(name):
            return ""
        return docker.container.logs(name, tail=lines) or ""
    except Exception:
        logger.exception("deployer: logs fetch failed for bot_id={}", bot_id)
        return ""
