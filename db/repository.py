from datetime import datetime, timezone

from sqlalchemy import select

from db.database import get_session
from db.models import BotConfig, Client, ConsentLog, Subscription


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
) -> BotConfig:
    async with get_session() as session:
        bot = BotConfig(
            client_id=client_id,
            bot_type=bot_type,
            bot_name=bot_name,
            bot_token="",
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


async def create_subscription(
    client_id: int, payment_id: str, plan: str
) -> Subscription:
    async with get_session() as session:
        sub = Subscription(
            client_id=client_id,
            yukassa_payment_id=payment_id,
            status="pending",
            plan=plan,
        )
        session.add(sub)
        await session.flush()
        session.expunge(sub)
        return sub


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
