# Bot Factory — SaaS фабрика Telegram-ботов

## Стек
- Python 3.11, aiogram 3.x, anthropic SDK
- PostgreSQL + Redis (Docker локально, Selectel в проде)
- Деплой: Docker + Selectel (серверы в РФ, 152-ФЗ)
- Платежи: ЮKassa

## Структура
- main.py — Telegram intake бот
- pipeline.py — оркестратор цепочки агентов
- agents/ — аналитик, архитектор, промпт-инженер, строитель
- db/ — модели SQLAlchemy, миграции Alembic
- deployer.py — деплой в Docker
- billing.py — ЮKassa подписки

## Правила кода
- Комментарии и переменные на английском
- Секреты только через .env
- Стиль: black + isort
