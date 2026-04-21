"""Daily token-limit alert scheduler.

Runs once per day at 10:00 Europe/Moscow, iterates clients who still have
limit_alerts_enabled and an active subscription, and DMs anyone under 30%
of their monthly budget. Under 10% is escalated to a critical-tone message.

The scheduler is started from main.py during startup and stopped during
graceful shutdown. APScheduler's AsyncIOScheduler uses the asyncio loop
it's started on, so lifecycle is tied to the main event loop.
"""

from loguru import logger

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db.repository import get_clients_for_limit_alerts

SCHEDULER_TIMEZONE = "Europe/Moscow"
DAILY_HOUR = 10
DAILY_MINUTE = 0
CRITICAL_PCT = 0.10


def _format_pct(pct_left: float) -> int:
    return max(0, min(100, round(pct_left * 100)))


def _build_message(pct_left: float, days_left: int | None) -> str:
    pct = _format_pct(pct_left)
    if days_left is None:
        horizon = "при текущем расходе — сложно оценить, нет данных за неделю"
    elif days_left == 0:
        horizon = "при текущем расходе — токены могут закончиться сегодня"
    else:
        word = (
            "день"
            if days_left % 10 == 1 and days_left % 100 != 11
            else (
                "дня"
                if 2 <= days_left % 10 <= 4
                and not (12 <= days_left % 100 <= 14)
                else "дней"
            )
        )
        horizon = f"при текущем расходе хватит на {days_left} {word}"
    if pct_left < CRITICAL_PCT:
        prefix = "🔴 Критично: осталось"
    else:
        prefix = "⚠️ У вас осталось"
    return (
        f"{prefix} {pct}% токенов на этот месяц.\n"
        f"{horizon[0].upper() + horizon[1:]}.\n"
        "/subscribe для апгрейда тарифа."
    )


async def send_limit_alerts(bot) -> None:
    """Invoked by the scheduler. Any per-client failure is logged and
    does not abort the batch — one bad chat shouldn't starve the rest."""
    try:
        candidates = await get_clients_for_limit_alerts()
    except Exception:
        logger.exception("alerts: failed to load candidates")
        return

    if not candidates:
        logger.info("alerts: no clients under 30%%")
        return

    logger.info("alerts: dispatching to {} client(s)", len(candidates))
    sent = 0
    for c in candidates:
        text = _build_message(c["pct_left"], c["days_left"])
        try:
            await bot.send_message(c["telegram_id"], text)
            sent += 1
        except Exception:
            logger.exception(
                "alerts: send failed tg_id={}", c["telegram_id"]
            )
    logger.info("alerts: delivered {}/{}", sent, len(candidates))


def start_alerts_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
    scheduler.add_job(
        send_limit_alerts,
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        args=[bot],
        id="daily_limit_alerts",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "alerts: scheduler started (daily at {:02d}:{:02d} {})",
        DAILY_HOUR,
        DAILY_MINUTE,
        SCHEDULER_TIMEZONE,
    )
    return scheduler
