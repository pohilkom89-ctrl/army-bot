"""Scheduled broadcast runner.

Runs every minute via APScheduler. Picks up all pending ScheduledBroadcast
rows whose send_at has passed and sends them to the bot's subscribers.
"""
import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from db.repository import (
    get_bot_by_id_any,
    get_pending_broadcasts,
    get_subscriber_ids,
    mark_broadcast_sent,
)

# Telegram rate limit is 30 messages/sec; stay well under it.
_BATCH_SIZE = 25


async def run_pending_broadcasts(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    try:
        pending = await get_pending_broadcasts(before=now)
    except Exception:
        logger.exception("broadcasts: failed to load pending rows")
        return

    if not pending:
        return

    logger.info("broadcasts: {} job(s) to dispatch", len(pending))

    for broadcast in pending:
        bot_cfg = await get_bot_by_id_any(broadcast.bot_id)
        if bot_cfg is None:
            logger.warning("broadcasts: bot_id={} not found — skipping", broadcast.bot_id)
            await mark_broadcast_sent(broadcast.id, 0, 0)
            continue

        subscriber_ids = await get_subscriber_ids(broadcast.bot_id)
        if not subscriber_ids:
            logger.info("broadcasts: bot_id={} has no subscribers", broadcast.bot_id)
            await mark_broadcast_sent(broadcast.id, 0, 0)
            continue

        broadcast_bot = Bot(token=bot_cfg.bot_token)
        sent = failed = 0
        try:
            for i, tg_id in enumerate(subscriber_ids):
                try:
                    await broadcast_bot.send_message(tg_id, broadcast.message_text)
                    sent += 1
                except Exception:
                    failed += 1
                if (i + 1) % _BATCH_SIZE == 0:
                    await asyncio.sleep(1)
        finally:
            await broadcast_bot.session.close()

        await mark_broadcast_sent(broadcast.id, sent, failed)
        logger.info(
            "broadcasts: done bot_id={} sent={} failed={}",
            broadcast.bot_id,
            sent,
            failed,
        )


def attach_broadcasts_scheduler(scheduler, bot: Bot) -> None:
    scheduler.add_job(
        run_pending_broadcasts,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="scheduled_broadcasts",
        replace_existing=True,
    )
    logger.info("broadcasts: scheduler attached (every 1 min)")
