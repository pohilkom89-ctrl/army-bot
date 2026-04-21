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
