from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

EMBEDDING_DIM = 1536


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    # PII: nullable for 152-ФЗ data minimization
    username = Column(String(64), nullable=True)

    consent_given = Column(Boolean, nullable=False, default=False)
    consent_at = Column(DateTime(timezone=True), nullable=True)
    consent_text = Column(Text, nullable=True)

    data_deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    bots = relationship(
        "BotConfig", back_populates="client", cascade="all, delete-orphan"
    )
    consents = relationship(
        "ConsentLog", back_populates="client", cascade="all, delete-orphan"
    )
    subscriptions = relationship(
        "Subscription", back_populates="client", cascade="all, delete-orphan"
    )


class BotConfig(Base):
    __tablename__ = "bot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    bot_name = Column(String(128), nullable=False)
    # parser | seller | content | support
    bot_type = Column(String(32), nullable=False)
    bot_token = Column(String(128), nullable=False)
    system_prompt = Column(Text, nullable=False)
    config_json = Column(JSON, nullable=False, default=dict)

    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    client = relationship("Client", back_populates="bots")


class ConsentLog(Base):
    __tablename__ = "consent_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # given | revoked
    action = Column(String(16), nullable=False)
    ip_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    client = relationship("Client", back_populates="consents")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    yukassa_payment_id = Column(String(64), nullable=True, index=True)
    # pending | active | canceled | expired
    status = Column(String(32), nullable=False)
    # Billing cycle: monthly | yearly
    plan = Column(String(32), nullable=False)
    # Feature tier: starter | pro | business
    tier = Column(String(32), nullable=False, default="starter")

    # Token bucket. tokens_limit=NULL means unlimited (business tier).
    tokens_limit = Column(Integer, nullable=True)
    tokens_used = Column(Integer, nullable=False, default=0)
    tokens_reset_at = Column(DateTime(timezone=True), nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    client = relationship("Client", back_populates="subscriptions")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bot_id = Column(
        Integer,
        ForeignKey("bot_configs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    content = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM), nullable=False)
    source = Column(String(256), nullable=True)
    chunk_index = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bot_id = Column(
        Integer,
        ForeignKey("bot_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # user | assistant
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    tokens_used = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class TokenLog(Base):
    __tablename__ = "token_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bot_id = Column(
        Integer,
        ForeignKey("bot_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    tokens_in = Column(Integer, nullable=False)
    tokens_out = Column(Integer, nullable=False)
    model = Column(String(64), nullable=False)
    cost_usd = Column(Float, nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
