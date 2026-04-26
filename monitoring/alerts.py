"""Periodic health-check job + Telegram alert dispatch.

Runs every 5 minutes inside the same APScheduler instance that
services.alerts created (one scheduler, one event loop, less surface).
Calls /health/full locally; on failure (HTTP non-200 or transport
error) DMs every admin in ADMIN_TELEGRAM_IDS.

Deduplication: in-process dict keyed by failure signature (which checks
failed). Same signature within _DEDUP_WINDOW = 1h sends only once.
A different signature (e.g. postgres just dropped after containers
were already down) re-alerts. Process restart resets the dedup map —
desirable because ops want to know the service rebooted into a
degraded state.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from loguru import logger

from apscheduler.triggers.cron import CronTrigger

from config import ADMIN_TELEGRAM_IDS

_HEALTH_INTERVAL_MINUTES = 5
_DEDUP_WINDOW = timedelta(hours=1)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

_last_alert: dict[str, datetime] = {}


def _failure_signature(payload: dict) -> str:
    """Stable identifier for a failure mode — sorted list of failed
    check names. 'postgres' alone, 'postgres,redis', etc. Lets a
    different failure profile re-alert immediately even if last alert
    was within the dedup window."""
    failed = sorted(
        name
        for name, c in (payload.get("checks") or {}).items()
        if isinstance(c, dict) and not c.get("ok")
    )
    return ",".join(failed) or "unknown"


def _should_send(signature: str, now: datetime) -> bool:
    last = _last_alert.get(signature)
    if last is None:
        return True
    return (now - last) >= _DEDUP_WINDOW


async def _check_health() -> Optional[dict]:
    """Hit the local readiness endpoint. Returns:
      None  — HTTP 200, all checks passing
      dict  — non-200 response (with parsed JSON) or transport failure
              (with synthesised payload so the alert path always has data)"""
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    url = f"http://127.0.0.1:{port}/health/full"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = {"status": "fail", "raw": (await resp.text())[:1000]}
                if resp.status == 200:
                    return None
                body["_http_status"] = resp.status
                return body
    except Exception as e:
        return {
            "status": "fail",
            "_http_status": 0,
            "checks": {
                "http": {"ok": False, "detail": f"{type(e).__name__}: {e}"}
            },
        }


async def health_check_job(bot) -> None:
    payload = await _check_health()
    if payload is None:
        return

    now = datetime.now(timezone.utc)
    sig = _failure_signature(payload)
    if not _should_send(sig, now):
        logger.info(
            "health_alert: deduped (same signature {!r} within {}h)",
            sig,
            _DEDUP_WINDOW.total_seconds() // 3600,
        )
        return
    _last_alert[sig] = now

    detail = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(detail) > 3500:
        detail = detail[:3500] + "\n... (truncated)"

    msg = (
        "🚨 ArmyBots: проверка здоровья не прошла.\n\n"
        f"<pre>{detail}</pre>"
    )

    if not ADMIN_TELEGRAM_IDS:
        logger.warning(
            "health_alert: degraded but ADMIN_TELEGRAM_IDS empty — no recipient"
        )
        return

    sent = 0
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await bot.send_message(admin_id, msg, parse_mode="HTML")
            sent += 1
        except Exception:
            logger.exception(
                "health_alert: send failed tg_id={}", admin_id
            )
    logger.warning(
        "health_alert: dispatched signature={!r} to {}/{} admins",
        sig,
        sent,
        len(ADMIN_TELEGRAM_IDS),
    )


def attach_health_monitor(scheduler, bot) -> None:
    """Add the health check job to an existing AsyncIOScheduler. Use
    after start_alerts_scheduler so we share one scheduler instance."""
    scheduler.add_job(
        health_check_job,
        CronTrigger(minute=f"*/{_HEALTH_INTERVAL_MINUTES}"),
        args=[bot],
        id="health_monitor",
        replace_existing=True,
    )
    logger.info(
        "health_alert: monitor attached (every {} minutes)",
        _HEALTH_INTERVAL_MINUTES,
    )
