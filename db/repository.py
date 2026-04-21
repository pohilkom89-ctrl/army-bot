from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import func, select

from config import PLANS
from db.database import get_session
from db.models import (
    BotConfig,
    ChatHistory,
    Client,
    ConsentLog,
    KnowledgeChunk,
    Subscription,
    TokenLog,
)

# OpenRouter pricing per 1M tokens (USD). Keys are the exact model slugs we
# pass to the OpenAI SDK, so the lookup in log_tokens is a direct match. If a
# new model is introduced, add it here — otherwise cost falls back to 0 and a
# warning is logged.
MODEL_PRICING_USD_PER_1M: dict[str, float] = {
    "meta-llama/llama-3.3-70b-instruct": 0.12,
    "deepseek/deepseek-chat-v3.1": 0.28,
    "qwen/qwen3-235b-a22b": 0.54,
}

TOKEN_RESET_PERIOD = timedelta(days=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_client(
    telegram_id: int, username: str | None = None
) -> Client:
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one_or_none()
        if client is None:
            client = Client(telegram_id=telegram_id, username=username)
            session.add(client)
            await session.flush()
        elif username and client.username != username and not client.data_deleted:
            client.username = username
        session.expunge(client)
        return client


async def save_consent(telegram_id: int, consent_text: str) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one()

        now = _utcnow()
        client.consent_given = True
        client.consent_at = now
        client.consent_text = consent_text

        session.add(ConsentLog(client_id=client.id, action="given"))


async def revoke_consent(telegram_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one()

        client.consent_given = False
        session.add(ConsentLog(client_id=client.id, action="revoked"))


async def anonymize_user(telegram_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one()

        client.username = None
        client.data_deleted = True
        client.deleted_at = _utcnow()


async def save_bot_config(
    client_id: int,
    bot_type: str,
    bot_name: str,
    system_prompt: str,
    config: dict,
    bot_token: str,
) -> BotConfig:
    async with get_session() as session:
        bot = BotConfig(
            client_id=client_id,
            bot_type=bot_type,
            bot_name=bot_name,
            bot_token=bot_token,
            system_prompt=system_prompt,
            config_json=config,
        )
        session.add(bot)
        await session.flush()
        session.expunge(bot)
        return bot


async def get_client_bots(client_id: int) -> list[BotConfig]:
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig)
            .where(BotConfig.client_id == client_id)
            .order_by(BotConfig.created_at.desc())
        )
        bots = list(result.scalars().all())
        for bot in bots:
            session.expunge(bot)
        return bots


async def get_bot_by_id(bot_id: int, client_id: int) -> BotConfig | None:
    """Fetch a bot enforcing ownership — returns None if the bot doesn't
    belong to this client. Callers should treat None as 'not found'."""
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        bot = result.scalar_one_or_none()
        if bot is not None:
            session.expunge(bot)
        return bot


async def get_bot_by_id_any(bot_id: int) -> BotConfig | None:
    """Fetch a bot by PK without ownership check. Used by infrastructure
    callers (deployer, migration scripts) that already know the bot_id
    from an out-of-band source. User-facing code paths should prefer
    get_bot_by_id(bot_id, client_id)."""
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(BotConfig.id == bot_id)
        )
        bot = result.scalar_one_or_none()
        if bot is not None:
            session.expunge(bot)
        return bot


async def update_bot_config(
    bot_id: int, client_id: int, key: str, value: Any
) -> bool:
    """Write a key into BotConfig.config_json. Returns False if the bot
    is not owned by this client. SQLAlchemy's JSON column requires full
    dict reassignment to register mutation — don't mutate in place."""
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        bot = result.scalar_one_or_none()
        if bot is None:
            return False
        cfg = dict(bot.config_json or {})
        cfg[key] = value
        bot.config_json = cfg
        return True


async def update_bot_system_prompt(
    bot_id: int, client_id: int, system_prompt: str
) -> bool:
    """Update BotConfig.system_prompt — the column the runtime reads.
    Callers that only change config_json won't affect runtime behaviour
    unless they also regenerate the prompt via pipeline.regenerate_system_prompt
    and persist it through this function."""
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        bot = result.scalar_one_or_none()
        if bot is None:
            return False
        bot.system_prompt = system_prompt
        return True


async def set_bot_status(
    bot_id: int, client_id: int, status: str
) -> bool:
    """Update BotConfig.status. Returns False if the bot is not owned
    by the client or the status value is rejected."""
    if status not in ("active", "paused"):
        raise ValueError(f"Unknown bot status: {status}")
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        bot = result.scalar_one_or_none()
        if bot is None:
            return False
        bot.status = status
        return True


async def delete_bot(bot_id: int, client_id: int) -> bool:
    """Hard-delete BotConfig. ChatHistory and KnowledgeChunk are removed
    via FK CASCADE; TokenLog rows SET NULL on bot_id (kept for billing).
    Returns False if the bot is not owned by this client."""
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        bot = result.scalar_one_or_none()
        if bot is None:
            return False
        await session.delete(bot)
        return True


async def get_bot_stats(
    bot_id: int, client_id: int
) -> dict[str, Any] | None:
    """Per-bot usage summary for the dashboard card."""
    async with get_session() as session:
        bot_result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        if bot_result.scalar_one_or_none() is None:
            return None

        # Request count = user messages.
        req_count = await session.scalar(
            select(func.count(ChatHistory.id)).where(
                ChatHistory.bot_id == bot_id,
                ChatHistory.role == "user",
            )
        )

        # Tokens = sum across TokenLog (survives bot deletion via SET NULL,
        # but here we only care about live bots).
        tokens_used = await session.scalar(
            select(
                func.coalesce(
                    func.sum(TokenLog.tokens_in + TokenLog.tokens_out), 0
                )
            ).where(TokenLog.bot_id == bot_id)
        )

        # Average assistant reply length in characters.
        avg_reply_len = await session.scalar(
            select(
                func.coalesce(func.avg(func.length(ChatHistory.content)), 0)
            ).where(
                ChatHistory.bot_id == bot_id,
                ChatHistory.role == "assistant",
            )
        )

        last_activity = await session.scalar(
            select(func.max(ChatHistory.created_at)).where(
                ChatHistory.bot_id == bot_id
            )
        )

        # Knowledge base: chunks + distinct sources.
        kb_chunks = await session.scalar(
            select(func.count(KnowledgeChunk.id)).where(
                KnowledgeChunk.bot_id == bot_id
            )
        )
        kb_sources = await session.scalar(
            select(
                func.count(func.distinct(KnowledgeChunk.source))
            ).where(
                KnowledgeChunk.bot_id == bot_id,
                KnowledgeChunk.source.is_not(None),
            )
        )

        return {
            "request_count": int(req_count or 0),
            "tokens_used": int(tokens_used or 0),
            "avg_reply_len": int(avg_reply_len or 0),
            "last_activity": last_activity,
            "kb_chunks": int(kb_chunks or 0),
            "kb_sources": int(kb_sources or 0),
        }


async def create_subscription(
    client_id: int,
    payment_id: str,
    plan: str,
    status: str = "pending",
    started_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    tier: str = "starter",
    tokens_reset_at: Optional[datetime] = None,
) -> Subscription:
    if tier not in PLANS:
        raise ValueError(f"Unknown tier: {tier}")
    tokens_limit = PLANS[tier]["tokens_limit"]
    async with get_session() as session:
        sub = Subscription(
            client_id=client_id,
            yukassa_payment_id=payment_id,
            status=status,
            plan=plan,
            tier=tier,
            tokens_limit=tokens_limit,
            tokens_used=0,
            tokens_reset_at=tokens_reset_at,
            started_at=started_at,
            expires_at=expires_at,
        )
        session.add(sub)
        await session.flush()
        session.expunge(sub)
        return sub


def _maybe_reset_tokens(sub: Subscription, now: datetime) -> None:
    """Zero tokens_used and advance reset_at if the period has elapsed."""
    if sub.tokens_reset_at is None:
        return
    if sub.tokens_reset_at <= now:
        sub.tokens_used = 0
        sub.tokens_reset_at = now + TOKEN_RESET_PERIOD


async def _active_subscription(session, client_id: int, now: datetime):
    result = await session.execute(
        select(Subscription)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def log_tokens(
    client_id: int,
    bot_id: Optional[int],
    tokens_in: int,
    tokens_out: int,
    model: str,
) -> None:
    price = MODEL_PRICING_USD_PER_1M.get(model)
    if price is None:
        logger.warning("log_tokens: unknown model '{}' — cost set to 0", model)
        price = 0.0
    total = tokens_in + tokens_out
    cost_usd = (total / 1_000_000.0) * price

    async with get_session() as session:
        session.add(
            TokenLog(
                client_id=client_id,
                bot_id=bot_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=model,
                cost_usd=cost_usd,
            )
        )
        now = _utcnow()
        sub = await _active_subscription(session, client_id, now)
        if sub is not None:
            _maybe_reset_tokens(sub, now)
            sub.tokens_used = (sub.tokens_used or 0) + total


async def get_usage_stats(client_id: int) -> dict[str, Any]:
    async with get_session() as session:
        now = _utcnow()
        sub = await _active_subscription(session, client_id, now)

        cost_result = await session.execute(
            select(func.coalesce(func.sum(TokenLog.cost_usd), 0.0)).where(
                TokenLog.client_id == client_id
            )
        )
        cost_usd_total = float(cost_result.scalar_one() or 0.0)

        if sub is None:
            return {
                "tokens_used": 0,
                "tokens_limit": 0,
                "tokens_left": 0,
                "cost_usd_total": cost_usd_total,
                "reset_at": None,
                "tier": None,
            }

        _maybe_reset_tokens(sub, now)
        tokens_used = sub.tokens_used or 0
        tokens_limit = sub.tokens_limit
        if tokens_limit is None:
            tokens_left = None
        else:
            tokens_left = max(0, tokens_limit - tokens_used)

        return {
            "tokens_used": tokens_used,
            "tokens_limit": tokens_limit,
            "tokens_left": tokens_left,
            "cost_usd_total": cost_usd_total,
            "reset_at": sub.tokens_reset_at,
            "tier": sub.tier,
        }


async def check_and_update_tokens(client_id: int, tokens_needed: int) -> bool:
    async with get_session() as session:
        now = _utcnow()
        sub = await _active_subscription(session, client_id, now)
        if sub is None:
            return False

        _maybe_reset_tokens(sub, now)

        if sub.tokens_limit is None:
            sub.tokens_used = (sub.tokens_used or 0) + tokens_needed
            return True

        if (sub.tokens_used or 0) + tokens_needed > sub.tokens_limit:
            return False

        sub.tokens_used = (sub.tokens_used or 0) + tokens_needed
        return True


async def save_chat_message(
    client_id: int,
    bot_id: int,
    role: str,
    content: str,
    tokens: int = 0,
) -> None:
    if role not in ("user", "assistant"):
        raise ValueError(f"Unknown chat role: {role}")
    async with get_session() as session:
        session.add(
            ChatHistory(
                client_id=client_id,
                bot_id=bot_id,
                role=role,
                content=content,
                tokens_used=tokens,
            )
        )


async def get_chat_history(
    client_id: int, bot_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    async with get_session() as session:
        result = await session.execute(
            select(ChatHistory)
            .where(
                ChatHistory.client_id == client_id,
                ChatHistory.bot_id == bot_id,
            )
            .order_by(ChatHistory.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
    rows.reverse()
    return [
        {"role": r.role, "content": r.content, "created_at": r.created_at}
        for r in rows
    ]


async def set_limit_alerts(telegram_id: int, enabled: bool) -> bool:
    """Flip the per-client toggle for daily limit alerts."""
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one_or_none()
        if client is None:
            return False
        client.limit_alerts_enabled = enabled
        return True


async def get_limit_alerts_enabled(telegram_id: int) -> bool:
    """Default True: treat missing client as enabled so new users get alerts."""
    async with get_session() as session:
        result = await session.execute(
            select(Client.limit_alerts_enabled).where(
                Client.telegram_id == telegram_id
            )
        )
        row = result.scalar_one_or_none()
        return True if row is None else bool(row)


async def get_usage_by_bot(
    client_id: int, period_start: datetime
) -> list[dict[str, Any]]:
    """Per-bot token totals since period_start. Rows whose bot_id is
    NULL (bot deleted — SET NULL cascade) are skipped: we only surface
    breakdowns the client can still act on."""
    async with get_session() as session:
        result = await session.execute(
            select(
                BotConfig.id,
                BotConfig.bot_name,
                BotConfig.bot_type,
                func.coalesce(
                    func.sum(TokenLog.tokens_in + TokenLog.tokens_out), 0
                ).label("tokens"),
            )
            .join(TokenLog, TokenLog.bot_id == BotConfig.id)
            .where(
                TokenLog.client_id == client_id,
                TokenLog.created_at >= period_start,
            )
            .group_by(BotConfig.id, BotConfig.bot_name, BotConfig.bot_type)
            .order_by(func.sum(
                TokenLog.tokens_in + TokenLog.tokens_out
            ).desc())
        )
        rows = result.all()
    return [
        {
            "bot_id": r.id,
            "bot_name": r.bot_name,
            "bot_type": r.bot_type,
            "tokens": int(r.tokens or 0),
        }
        for r in rows
    ]


async def get_usage_by_model(
    client_id: int, period_start: datetime
) -> list[dict[str, Any]]:
    """Per-model token + cost breakdown since period_start. Used by
    /usage to show the multi-LLM routing split (cheap / balanced / smart)."""
    async with get_session() as session:
        result = await session.execute(
            select(
                TokenLog.model,
                func.coalesce(
                    func.sum(TokenLog.tokens_in + TokenLog.tokens_out), 0
                ).label("tokens"),
                func.coalesce(func.sum(TokenLog.cost_usd), 0.0).label("cost"),
            )
            .where(
                TokenLog.client_id == client_id,
                TokenLog.created_at >= period_start,
            )
            .group_by(TokenLog.model)
            .order_by(func.sum(
                TokenLog.tokens_in + TokenLog.tokens_out
            ).desc())
        )
        rows = result.all()
    return [
        {
            "model": r.model,
            "tokens": int(r.tokens or 0),
            "cost_usd": float(r.cost or 0.0),
        }
        for r in rows
    ]


async def get_daily_usage(
    client_id: int, days: int = 14
) -> list[dict[str, Any]]:
    """Token totals bucketed by UTC day for the last `days` days. Missing
    days are filled with zero so the caller can render a continuous chart."""
    now = _utcnow()
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async with get_session() as session:
        result = await session.execute(
            select(
                func.date_trunc("day", TokenLog.created_at).label("d"),
                func.coalesce(
                    func.sum(TokenLog.tokens_in + TokenLog.tokens_out), 0
                ).label("tokens"),
            )
            .where(
                TokenLog.client_id == client_id,
                TokenLog.created_at >= start,
            )
            .group_by("d")
        )
        by_date: dict[datetime, int] = {
            row.d: int(row.tokens or 0) for row in result.all()
        }

    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        out.append({"date": day, "tokens": by_date.get(day, 0)})
    return out


async def get_usage_trend(client_id: int) -> dict[str, Any]:
    """today / yesterday / this_week (last 7 days) / last_week (8-14 days ago)
    with week-over-week growth as percent. Negative growth is reported."""
    now = _utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    this_week_start = today_start - timedelta(days=6)
    last_week_start = today_start - timedelta(days=13)

    async def _sum(since, until) -> int:
        async with get_session() as session:
            result = await session.execute(
                select(
                    func.coalesce(
                        func.sum(TokenLog.tokens_in + TokenLog.tokens_out),
                        0,
                    )
                ).where(
                    TokenLog.client_id == client_id,
                    TokenLog.created_at >= since,
                    TokenLog.created_at < until,
                )
            )
            return int(result.scalar_one() or 0)

    today_end = today_start + timedelta(days=1)
    today = await _sum(today_start, today_end)
    yesterday = await _sum(yesterday_start, today_start)
    this_week = await _sum(this_week_start, today_end)
    last_week = await _sum(last_week_start, this_week_start)

    if last_week > 0:
        growth_pct = round(100 * (this_week - last_week) / last_week)
    else:
        growth_pct = None

    return {
        "today": today,
        "yesterday": yesterday,
        "this_week": this_week,
        "last_week": last_week,
        "growth_pct": growth_pct,
    }


async def get_clients_for_limit_alerts() -> list[dict[str, Any]]:
    """Return alert candidates: clients with alerts_enabled=True, an
    active subscription, finite tokens_limit, and ≥70% consumption
    (i.e. ≤30% tokens remaining). Also returns days_left projected
    from the last 7 days' average rate so the scheduler can word the
    message appropriately."""
    now = _utcnow()
    week_start = now - timedelta(days=7)

    out: list[dict[str, Any]] = []
    async with get_session() as session:
        result = await session.execute(
            select(Client, Subscription)
            .join(
                Subscription,
                Subscription.client_id == Client.id,
            )
            .where(
                Client.limit_alerts_enabled.is_(True),
                Client.data_deleted.is_(False),
                Subscription.status == "active",
                Subscription.expires_at > now,
                Subscription.tokens_limit.is_not(None),
            )
        )
        rows = list(result.all())

        for client, sub in rows:
            _maybe_reset_tokens(sub, now)
            limit = sub.tokens_limit
            used = sub.tokens_used or 0
            if limit <= 0:
                continue
            pct_left = max(0, limit - used) / limit
            if pct_left > 0.30:
                continue

            week_result = await session.execute(
                select(
                    func.coalesce(
                        func.sum(TokenLog.tokens_in + TokenLog.tokens_out),
                        0,
                    )
                ).where(
                    TokenLog.client_id == client.id,
                    TokenLog.created_at >= week_start,
                )
            )
            week_tokens = int(week_result.scalar_one() or 0)
            avg_daily = week_tokens / 7 if week_tokens else 0
            tokens_left = max(0, limit - used)
            if avg_daily > 0:
                days_left = int(tokens_left / avg_daily)
            else:
                days_left = None

            out.append(
                {
                    "client_id": client.id,
                    "telegram_id": client.telegram_id,
                    "pct_left": pct_left,
                    "tokens_left": tokens_left,
                    "days_left": days_left,
                }
            )
    return out


async def get_active_subscription(client_id: int) -> Subscription | None:
    async with get_session() as session:
        now = _utcnow()
        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.client_id == client_id,
                Subscription.status == "active",
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
        sub = result.scalar_one_or_none()
        if sub is not None:
            session.expunge(sub)
        return sub
