import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://admin:changeme@localhost:5432/botfactory",
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

SessionFactory = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    session = SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    from db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
