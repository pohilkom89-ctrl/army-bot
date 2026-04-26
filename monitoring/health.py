"""HTTP health endpoints for the factory.

  GET /health       — liveness. Always 200 if the process can serve.
                      Used by uptime pingers and Docker healthchecks.
  GET /health/full  — readiness. Runs DB / Redis / container checks.
                      Returns 200 when everything is OK, 500 otherwise,
                      with a JSON breakdown so monitoring/alerts.py can
                      ship the detail to admins.

Routes are added to the existing aiohttp app in webhook_server.build_app
via register_health_routes() — keeps lifecycle tied to the same server
and same port (8080), nothing to expose separately."""

import asyncio
import os
from typing import Any

from aiohttp import web
from loguru import logger
from sqlalchemy import text

from db.database import get_session


async def check_postgres() -> tuple[bool, str]:
    try:
        async with get_session() as session:
            result = await session.execute(text("SELECT 1"))
            value = result.scalar_one()
            if value != 1:
                return False, f"unexpected SELECT 1 → {value!r}"
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def check_redis() -> tuple[bool, str]:
    """Skipped if REDIS_URL is not set (no redis-using code in the
    factory yet — voice/RAG features will fill this in)."""
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return True, "skipped (REDIS_URL not set)"
    try:
        import redis.asyncio as aioredis  # local import — keeps health module light
    except ImportError:
        return True, "skipped (redis-py not installed)"
    client = aioredis.from_url(
        redis_url, socket_connect_timeout=3, socket_timeout=3
    )
    try:
        pong = await client.ping()
        if not pong:
            return False, "ping returned falsy"
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def check_client_containers() -> tuple[bool, dict[str, Any]]:
    """Every bot_client_* container must be 'running'. A stopped/exited
    container means a client's bot is offline — needs ops attention even
    if the factory itself is healthy. Containers in 'paused' Docker state
    are also flagged (Docker pause ≠ /mybots → pause which removes the
    container; if Docker says paused something external paused it)."""
    try:
        rows = await asyncio.to_thread(_list_bot_containers)
    except Exception as e:
        return False, {
            "detail": f"docker.list failed: {type(e).__name__}: {e}",
            "containers": {},
        }

    if not rows:
        return True, {"detail": "no client containers", "containers": {}}

    not_running = [n for n, s in rows.items() if s != "running"]
    if not_running:
        return False, {
            "detail": f"{len(not_running)}/{len(rows)} not running",
            "containers": rows,
        }
    return True, {"detail": f"{len(rows)} running", "containers": rows}


def _list_bot_containers() -> dict[str, str]:
    from python_on_whales import docker

    out: dict[str, str] = {}
    for c in docker.container.list(all=True, filters={"name": "bot_client_"}):
        name = getattr(c, "name", "") or ""
        if not name.startswith("bot_client_"):
            continue
        state = getattr(c, "state", None)
        status = (getattr(state, "status", "") or "unknown").lower()
        out[name] = status
    return out


async def liveness(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def readiness(request: web.Request) -> web.Response:
    pg_ok, pg_detail = await check_postgres()
    redis_ok, redis_detail = await check_redis()
    cont_ok, cont_detail = await check_client_containers()

    overall = pg_ok and redis_ok and cont_ok
    status_code = 200 if overall else 500

    payload = {
        "status": "ok" if overall else "fail",
        "checks": {
            "postgres": {"ok": pg_ok, "detail": pg_detail},
            "redis": {"ok": redis_ok, "detail": redis_detail},
            "containers": {"ok": cont_ok, **cont_detail},
        },
    }

    if not overall:
        logger.warning("health/full: NOT OK — {}", payload)

    return web.json_response(payload, status=status_code)


def register_health_routes(app: web.Application) -> None:
    app.router.add_get("/health", liveness)
    app.router.add_get("/health/full", readiness)
