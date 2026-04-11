import asyncio
import os
from pathlib import Path

from loguru import logger
from python_on_whales import docker

from db.repository import get_client_bots

BOTS_DIR = Path("bots")

# Minimal runtime image for generated bots. Pinned versions match requirements.txt
# so the generated bot behaves the same as the factory tested against.
DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \\
    aiogram==3.13.0 \\
    openai==2.31.0 \\
    loguru==0.7.3 \\
    python-dotenv==1.0.1

COPY main.py /app/main.py

CMD ["python", "main.py"]
"""


def _bot_dir(client_id: int) -> Path:
    return BOTS_DIR / str(client_id)


def _container_name(client_id: int) -> str:
    return f"bot_{client_id}"


def _image_tag(client_id: int) -> str:
    return f"bot_factory/bot_{client_id}:latest"


def prepare_bot_files(bot_code: str, client_id: int) -> Path:
    bot_dir = _bot_dir(client_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "main.py").write_text(bot_code, encoding="utf-8")
    generate_dockerfile(client_id)
    logger.info("deployer: prepared files for client_id={}", client_id)
    return bot_dir


def generate_dockerfile(client_id: int) -> Path:
    bot_dir = _bot_dir(client_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = bot_dir / "Dockerfile"
    dockerfile.write_text(DOCKERFILE_TEMPLATE, encoding="utf-8")
    logger.info("deployer: Dockerfile written for client_id={}", client_id)
    return dockerfile


async def deploy_bot(client_id: int) -> str:
    bots = await get_client_bots(client_id)
    if not bots:
        raise RuntimeError(f"deployer: no BotConfig for client_id={client_id}")
    bot_token = bots[0].bot_token
    if not bot_token:
        raise RuntimeError(
            f"deployer: bot_token is empty for client_id={client_id}"
        )

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key:
        raise RuntimeError("OPENROUTER_API_KEY is required to deploy bots")
    model_bots = os.getenv("OPENROUTER_MODEL_BOTS", "qwen/qwen3-235b-a22b")

    return await asyncio.to_thread(
        _deploy_sync, client_id, bot_token, openrouter_key, model_bots
    )


def _deploy_sync(
    client_id: int,
    bot_token: str,
    openrouter_key: str,
    model_bots: str,
) -> str:
    bot_dir = _bot_dir(client_id)
    if not (bot_dir / "main.py").exists():
        raise FileNotFoundError(
            f"deployer: {bot_dir / 'main.py'} not found — call prepare_bot_files first"
        )
    if not (bot_dir / "Dockerfile").exists():
        generate_dockerfile(client_id)

    name = _container_name(client_id)
    tag = _image_tag(client_id)

    logger.info("deployer: building image {}", tag)
    docker.build(context_path=str(bot_dir), tags=[tag])

    if docker.container.exists(name):
        logger.info("deployer: removing existing container {}", name)
        docker.container.remove(name, force=True)

    logger.info("deployer: starting container {}", name)
    container = docker.run(
        image=tag,
        name=name,
        detach=True,
        restart="unless-stopped",
        envs={
            "BOT_TOKEN": bot_token,
            "OPENROUTER_API_KEY": openrouter_key,
            "OPENROUTER_MODEL_BOTS": model_bots,
        },
    )
    container_id = getattr(container, "id", str(container))
    logger.info("deployer: container {} started (id={})", name, container_id)
    return container_id


def stop_bot(client_id: int) -> None:
    name = _container_name(client_id)
    if not docker.container.exists(name):
        logger.warning("deployer: container {} not found, nothing to stop", name)
        return
    docker.container.stop(name)
    docker.container.remove(name, force=True)
    logger.info("deployer: container {} stopped and removed", name)
