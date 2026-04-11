# Bot Factory

SaaS-фабрика Telegram-ботов. См. [CLAUDE.md](CLAUDE.md) для стека и правил.

## Локальный запуск

```bash
cp .env.example .env           # заполнить секреты
docker compose up -d            # postgres + redis
pip install -r requirements.txt
alembic upgrade head            # применить миграции
python main.py                  # запустить intake-бот
```

## Миграции (Alembic)

`DATABASE_URL` берётся из окружения (см. `.env`).

```bash
# Применить все миграции
alembic upgrade head

# Создать новую миграцию из изменений в db/models.py
alembic revision --autogenerate -m "описание изменения"

# Откатить последнюю миграцию
alembic downgrade -1
```
