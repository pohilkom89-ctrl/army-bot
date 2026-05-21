# Configuration and environment variable validation using Pydantic.

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Required for intake bot
    bot_token: str
    openrouter_api_key: str
    database_url: str
    redis_url: str

    # Optional with defaults
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model_agents: str = "deepseek/deepseek-chat-v3.1"
    openrouter_model_bots: str = "deepseek/deepseek-chat-v3.1"  # balanced tier
    admin_telegram_ids: str = ""

    # YooKassa billing
    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None
    billing_return_url: str = "https://t.me/"

    # Privacy / legal URLs (optional; shown in consent text and /my_data)
    privacy_policy_url: str | None = None
    terms_url: str | None = None

    # Postgres password for docker-compose
    postgres_password: str | None = None

    # For generated bots (usage reporting)
    factory_url: str = "http://host.docker.internal:8080"
    internal_api_key: str = ""

    # Image generation (FusionBrain)
    fusionbrain_api_key: str | None = None
    fusionbrain_secret_key: str | None = None

    # RAG embeddings
    embedding_base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "openai/text-embedding-3-small"

    # Deployment
    repo_url: str = "https://github.com/pohilkom89-ctrl/army-bot.git"

    # Support bot (optional — separate Telegram bot for customer support)
    support_bot_token: str | None = None
    support_log_chat_id: int | None = None         # all dialogues mirrored here
    support_escalation_chat_id: int | None = None  # escalations + /reply commands

    # Debug bot (optional — owner-only bot for analyzing and patching bugs)
    debug_bot_token: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"


# Global settings instance
settings = Settings()