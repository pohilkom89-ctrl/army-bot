from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import func, select, update

from config import BUSINESS_SOFT_CAP, PLANS, TRIAL_DAYS
from db.database import get_session
from db.models import (
    BotConfig,
    BotSubscriber,
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


async def check_consent(telegram_id: int) -> bool:
    """Check if user has given consent. Returns True if consent is given, False otherwise."""
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one_or_none()
        if client is None:
            return False
        return client.consent_given


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


async def clone_bot_config(
    source_bot_id: int,
    owner_client_id: int,
    new_token: str,
) -> BotConfig:
    """Duplicate a BotConfig owned by owner_client_id.

    Copies bot_type, system_prompt, config_json from source. The new bot
    gets the same name with ' (копия)' appended, a new token, and a fresh
    DB row. Raises ValueError if source bot is not found or not owned by
    owner_client_id.
    """
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == source_bot_id,
                BotConfig.client_id == owner_client_id,
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise ValueError(
                f"clone_bot_config: bot {source_bot_id} not found "
                f"for client {owner_client_id}"
            )
        clone = BotConfig(
            client_id=owner_client_id,
            bot_name=f"{source.bot_name} (копия)",
            bot_type=source.bot_type,
            bot_token=new_token,
            system_prompt=source.system_prompt,
            config_json=dict(source.config_json or {}),
            is_active=True,
            status="active",
        )
        session.add(clone)
        await session.flush()
        session.expunge(clone)
        return clone


async def get_client_bots(client_id: int) -> list[BotConfig]:
    async with get_session() as session:
        result = await session.execute(
            select(BotConfig)
            .where(
                BotConfig.client_id == client_id,
                BotConfig.merged_into.is_(None),
            )
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


async def rename_bot(bot_id: int, client_id: int, new_name: str) -> bool:
    """Update BotConfig.bot_name. Returns False if the bot is not owned by
    this client."""
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
        bot.bot_name = new_name
        return True


async def upsert_subscriber(bot_id: int, telegram_id: int) -> None:
    """Record that telegram_id has interacted with bot_id. Silently ignores
    duplicates — checks existence before inserting to stay dialect-neutral."""
    async with get_session() as session:
        existing = await session.scalar(
            select(BotSubscriber.id).where(
                BotSubscriber.bot_id == bot_id,
                BotSubscriber.telegram_id == telegram_id,
            )
        )
        if existing is None:
            session.add(BotSubscriber(bot_id=bot_id, telegram_id=telegram_id))


async def get_subscriber_ids(bot_id: int) -> list[int]:
    """Return all telegram_ids subscribed to bot_id."""
    async with get_session() as session:
        rows = await session.execute(
            select(BotSubscriber.telegram_id).where(BotSubscriber.bot_id == bot_id)
        )
        return [r for (r,) in rows.all()]


async def count_subscribers(bot_id: int) -> int:
    """Return subscriber count for bot_id."""
    async with get_session() as session:
        return int(await session.scalar(
            select(func.count(BotSubscriber.id)).where(BotSubscriber.bot_id == bot_id)
        ) or 0)


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


async def mark_bots_merged(source_ids: list[int], merged_into_id: int) -> None:
    """Mark source bots as absorbed by a merge. They become hidden from
    /mybots and no longer count against bots_limit."""
    async with get_session() as session:
        await session.execute(
            update(BotConfig)
            .where(BotConfig.id.in_(source_ids))
            .values(merged_into=merged_into_id)
        )


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


async def get_bot_analytics(
    bot_id: int, client_id: int
) -> dict[str, Any] | None:
    """Conversation analytics for the bot detail card.

    Returns None if the bot is not owned by this client. Fields:
    - unique_users: distinct client_ids who sent at least one message
    - total_messages: all user messages
    - messages_7d: user messages in the last 7 days
    - messages_30d: user messages in the last 30 days
    - peak_hour: hour of day (0–23 UTC) with most messages, or None
    - avg_messages_per_user: mean messages per unique user
    """
    async with get_session() as session:
        bot_result = await session.execute(
            select(BotConfig).where(
                BotConfig.id == bot_id,
                BotConfig.client_id == client_id,
            )
        )
        if bot_result.scalar_one_or_none() is None:
            return None

        now = datetime.now(timezone.utc)
        user_filter = [
            ChatHistory.bot_id == bot_id,
            ChatHistory.role == "user",
        ]

        unique_users = await session.scalar(
            select(func.count(func.distinct(ChatHistory.client_id))).where(*user_filter)
        )

        total_messages = await session.scalar(
            select(func.count(ChatHistory.id)).where(*user_filter)
        )

        messages_7d = await session.scalar(
            select(func.count(ChatHistory.id)).where(
                *user_filter,
                ChatHistory.created_at >= now - timedelta(days=7),
            )
        )

        messages_30d = await session.scalar(
            select(func.count(ChatHistory.id)).where(
                *user_filter,
                ChatHistory.created_at >= now - timedelta(days=30),
            )
        )

        # Peak hour: group by hour of day, pick the busiest.
        # func.extract works on both PostgreSQL and SQLite.
        peak_row = await session.execute(
            select(
                func.extract("hour", ChatHistory.created_at).label("hr"),
                func.count(ChatHistory.id).label("cnt"),
            )
            .where(*user_filter)
            .group_by(func.extract("hour", ChatHistory.created_at))
            .order_by(func.count(ChatHistory.id).desc())
            .limit(1)
        )
        peak = peak_row.first()
        peak_hour = int(peak.hr) if peak else None

        total = int(total_messages or 0)
        uniq = int(unique_users or 0)
        avg_per_user = round(total / uniq, 1) if uniq else 0.0

        return {
            "unique_users": uniq,
            "total_messages": total,
            "messages_7d": int(messages_7d or 0),
            "messages_30d": int(messages_30d or 0),
            "peak_hour": peak_hour,
            "avg_messages_per_user": avg_per_user,
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


async def count_client_bots(client_id: int) -> int:
    """Total BotConfig rows for this client (paused bots included).
    Merged-away bots (merged_into IS NOT NULL) are excluded."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(BotConfig)
            .where(
                BotConfig.client_id == client_id,
                BotConfig.merged_into.is_(None),
            )
        )
        return int(result.scalar_one() or 0)


async def count_simple_bots(client_id: int) -> int:
    """Count single-type bots (no merged_types key in config_json)."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(BotConfig)
            .where(
                BotConfig.client_id == client_id,
                BotConfig.merged_into.is_(None),
                BotConfig.config_json["merged_types"].is_(None),
            )
        )
        return int(result.scalar_one() or 0)


async def count_combo_bots(client_id: int) -> int:
    """Count multi-type bots (have merged_types key in config_json)."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(BotConfig)
            .where(
                BotConfig.client_id == client_id,
                BotConfig.merged_into.is_(None),
                BotConfig.config_json["merged_types"].isnot(None),
            )
        )
        return int(result.scalar_one() or 0)


async def find_subscription_by_payment_id(
    payment_id: str,
) -> Optional[Subscription]:
    """Lookup a subscription by its yukassa_payment_id. Used by the
    webhook handler to short-circuit duplicates (YooKassa retries
    delivery on non-2xx responses, and even on success some retries
    slip through). Returns None if no subscription exists for this
    payment yet."""
    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.yukassa_payment_id == payment_id
            )
        )
        return result.scalar_one_or_none()


def _maybe_reset_tokens(sub: Subscription, now: datetime) -> None:
    """Zero tokens_used and advance reset_at if the period has elapsed."""
    if sub.tokens_reset_at is None:
        return
    # SQLite returns naive datetimes; PostgreSQL returns aware. Normalise.
    reset_at = sub.tokens_reset_at
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    if reset_at <= now:
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
            tokens_limit = BUSINESS_SOFT_CAP
        tokens_left = max(0, tokens_limit - tokens_used)

        return {
            "tokens_used": tokens_used,
            "tokens_limit": tokens_limit,
            "tokens_left": tokens_left,
            "cost_usd_total": cost_usd_total,
            "reset_at": sub.tokens_reset_at,
            "tier": sub.tier,
            "plan": sub.plan,
        }


async def activate_trial(client_id: int) -> bool:
    """Provision a 7-day Pro trial for a new user.

    Returns True if the trial was created, False if the client already used
    a trial or already holds an active subscription.
    """
    now = _utcnow()
    async with get_session() as session:
        had_trial = await session.scalar(
            select(func.count()).select_from(Subscription).where(
                Subscription.client_id == client_id,
                Subscription.plan == "trial",
            )
        )
        if had_trial:
            return False

        active_sub = await _active_subscription(session, client_id, now)
        if active_sub is not None:
            return False

        expires_at = now + timedelta(days=TRIAL_DAYS)
        session.add(
            Subscription(
                client_id=client_id,
                yukassa_payment_id=None,
                status="active",
                plan="trial",
                tier="pro",
                tokens_limit=PLANS["pro"]["tokens_limit"],
                tokens_used=0,
                tokens_reset_at=expires_at,
                started_at=now,
                expires_at=expires_at,
            )
        )
        return True


async def check_and_update_tokens(client_id: int, tokens_needed: int) -> bool:
    async with get_session() as session:
        now = _utcnow()
        sub = await _active_subscription(session, client_id, now)
        if sub is None:
            return False

        _maybe_reset_tokens(sub, now)

        if sub.tokens_limit is None:
            if (sub.tokens_used or 0) + tokens_needed > BUSINESS_SOFT_CAP:
                return False
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
            )
        )
        rows = list(result.all())

        for client, sub in rows:
            _maybe_reset_tokens(sub, now)
            limit = sub.tokens_limit if sub.tokens_limit is not None else BUSINESS_SOFT_CAP
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


async def get_client_summary(telegram_id: int) -> dict[str, Any] | None:
    """Read-only summary of all data stored for this user. Used by /my_data."""
    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.telegram_id == telegram_id)
        )
        client = result.scalar_one_or_none()
        if client is None:
            return None

        bot_count = await session.scalar(
            select(func.count(BotConfig.id)).where(
                BotConfig.client_id == client.id
            )
        )

        now = _utcnow()
        sub_result = await session.execute(
            select(Subscription)
            .where(
                Subscription.client_id == client.id,
                Subscription.status == "active",
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
        sub = sub_result.scalar_one_or_none()

        chat_count = await session.scalar(
            select(func.count(ChatHistory.id)).where(
                ChatHistory.client_id == client.id
            )
        )

        kb_count = await session.scalar(
            select(func.count(KnowledgeChunk.id)).where(
                KnowledgeChunk.client_id == client.id
            )
        )

        return {
            "telegram_id": client.telegram_id,
            "username": client.username,
            "consent_given": client.consent_given,
            "consent_at": client.consent_at,
            "data_deleted": client.data_deleted,
            "created_at": client.created_at,
            "bot_count": int(bot_count or 0),
            "subscription_tier": sub.tier if sub else None,
            "subscription_expires_at": sub.expires_at if sub else None,
            "chat_message_count": int(chat_count or 0),
            "knowledge_chunk_count": int(kb_count or 0),
        }


async def get_admin_stats() -> dict[str, Any]:
    """Aggregate stats for the owner dashboard (/admin_stats)."""
    async with get_session() as session:
        now = _utcnow()

        # Active subscriptions grouped by tier and billing cycle
        subs_result = await session.execute(
            select(Subscription.tier, Subscription.plan, func.count(Subscription.id))
            .where(
                Subscription.status == "active",
                Subscription.expires_at > now,
            )
            .group_by(Subscription.tier, Subscription.plan)
        )
        sub_rows = subs_result.all()

        tier_counts: dict[str, int] = {}
        mrr: float = 0.0
        total_active = 0
        for tier, plan, cnt in sub_rows:
            tier_counts[tier] = tier_counts.get(tier, 0) + cnt
            total_active += cnt
            price_key = "price_monthly" if plan == "monthly" else "price_yearly"
            plan_cfg = PLANS.get(tier, {})
            monthly_price = (
                plan_cfg.get("price_monthly", 0)
                if plan == "monthly"
                else plan_cfg.get("price_yearly", 0) / 12
            )
            mrr += monthly_price * cnt

        # Total bots in the system
        bot_count = await session.scalar(
            select(func.count(BotConfig.id)).where(BotConfig.merged_into.is_(None))
        )

        # Total tokens used + cost across all active subscriptions this period
        tokens_total = await session.scalar(
            select(func.coalesce(func.sum(Subscription.tokens_used), 0)).where(
                Subscription.status == "active",
                Subscription.expires_at > now,
            )
        )
        cost_total = await session.scalar(
            select(func.coalesce(func.sum(TokenLog.cost_usd), 0.0))
        )

        # Top 5 clients by tokens_used in their current subscription
        top_result = await session.execute(
            select(Client.telegram_id, Client.username, Subscription.tokens_used)
            .join(Subscription, Subscription.client_id == Client.id)
            .where(
                Subscription.status == "active",
                Subscription.expires_at > now,
                Subscription.tokens_used > 0,
            )
            .order_by(Subscription.tokens_used.desc())
            .limit(5)
        )
        top_users = [
            {"telegram_id": r[0], "username": r[1], "tokens_used": r[2]}
            for r in top_result.all()
        ]

        # Total registered clients
        client_count = await session.scalar(
            select(func.count(Client.id)).where(Client.data_deleted.is_(False))
        )

        return {
            "total_active_subs": total_active,
            "tier_counts": tier_counts,
            "mrr": round(mrr),
            "bot_count": int(bot_count or 0),
            "client_count": int(client_count or 0),
            "tokens_total": int(tokens_total or 0),
            "cost_total_usd": float(cost_total or 0.0),
            "top_users": top_users,
        }
