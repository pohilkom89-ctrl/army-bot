# Bot Factory — деплой на Beget VPS

Пошаговая инструкция развёртывания фабрики ботов на production-сервере.

> **Beget VPS — российский провайдер, серверы в РФ, соответствует требованиям 152-ФЗ.**
> Домены и VPS покупаются в одном кабинете, что упрощает привязку DNS после покупки `armybots.ru`.

## Целевая конфигурация

| Параметр | Значение |
|---|---|
| Провайдер | Beget VPS (cloud.beget.com) |
| OS | Ubuntu 22.04 LTS |
| CPU | 2 vCPU |
| RAM | 4 GB |
| Регион | Россия (152-ФЗ) |
| Домен | IP-адрес сервера (на старте), позже `armybots.ru` |

---

## Шаг 1 — Первичная настройка сервера

В панели Beget создать VPS (Ubuntu 22.04 LTS, 2 vCPU / 4 GB RAM), скопировать публичный SSH-ключ в настройках. Подключиться по SSH под `root` и выполнить:

```bash
# Обновление системы
apt update && apt upgrade -y

# Базовые утилиты
apt install -y curl git ufw ca-certificates gnupg lsb-release

# Docker (официальный репозиторий)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Пользователь deploy (не root)
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

# UFW firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp       # SSH
ufw allow 80/tcp       # HTTP (Caddy ACME challenge — после покупки домена)
ufw allow 443/tcp      # HTTPS (YooKassa webhook — после покупки домена)
ufw allow 8080/tcp     # app (временно открыт наружу, пока webhook ходит по IP; закрыть после перевода на Caddy)
ufw --force enable

# Проверка
docker --version
docker compose version
ufw status
```

После этого отключиться от root и продолжить под `deploy`:

```bash
ssh deploy@<beget-vps-ip>
```

---

## Шаг 2 — Клонирование и настройка

```bash
cd ~
git clone https://github.com/pohilkom89-ctrl/army-bot.git
cd army-bot

# Конфиг окружения
cp .env.example .env
nano .env   # заполнить реальными значениями:
            #   BOT_TOKEN=<token от BotFather>
            #   OPENROUTER_API_KEY=<ключ>
            #   POSTGRES_PASSWORD=<сильный пароль>
            #   DATABASE_URL=postgresql+asyncpg://admin:<пароль>@localhost:5432/botfactory
            #   YUKASSA_SHOP_ID=<id>
            #   YUKASSA_SECRET_KEY=<секрет>
            #   WEBHOOK_PORT=8080

# Postgres + Redis
docker compose up -d
docker compose ps   # оба контейнера должны быть healthy

# Python окружение и миграции
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

---

## Шаг 3 — Запуск через systemd

Создать unit-файл:

```bash
sudo nano /etc/systemd/system/armybots.service
```

Содержимое:

```ini
[Unit]
Description=Bot Factory — intake bot + YooKassa webhook
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/home/deploy/army-bot
EnvironmentFile=/home/deploy/army-bot/.env
ExecStart=/home/deploy/army-bot/.venv/bin/python /home/deploy/army-bot/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=armybots

[Install]
WantedBy=multi-user.target
```

Активация:

```bash
sudo systemctl daemon-reload
sudo systemctl enable armybots
sudo systemctl start armybots
sudo systemctl status armybots
```

На этом этапе intake-бот работает (polling к Telegram), webhook-сервер слушает `http://<beget-vps-ip>:8080/webhook/yukassa`. YooKassa **не примет HTTP-webhook** — до покупки домена и настройки Caddy тестировать оплату можно только через ручной вызов `/webhook/yukassa` с IP-адреса из allowlist YooKassa.

---

## Шаг 4 — HTTPS через Caddy (выполнить после покупки домена `armybots.ru`)

> **Этот шаг выполняется только после того, как домен `armybots.ru` куплен через Beget и A-запись направлена на IP VPS.** YooKassa требует HTTPS-эндпоинт для webhook; Caddy даёт Let's Encrypt-сертификат автоматически.

**Подготовка DNS (в панели Beget):**
1. Купить домен `armybots.ru`.
2. В разделе DNS создать A-запись `armybots.ru` → IP VPS.
3. Дождаться прогрева DNS (проверить `dig armybots.ru` с VPS).

**Установка Caddy:**

```bash
# Официальный репозиторий Caddy
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

Caddyfile:

```bash
sudo nano /etc/caddy/Caddyfile
```

```caddy
armybots.ru {
    encode gzip
    reverse_proxy localhost:8080
    log {
        output file /var/log/caddy/armybots.log
    }
}
```

Перезапуск:

```bash
sudo systemctl reload caddy
sudo systemctl status caddy
```

Проверка:

```bash
curl -I https://armybots.ru/webhook/yukassa
```

После получения сертификата:
1. Закрыть 8080 наружу: `sudo ufw delete allow 8080/tcp` (Caddy ходит на `localhost:8080`, извне порт больше не нужен).
2. В личном кабинете YooKassa прописать webhook URL `https://armybots.ru/webhook/yukassa`.

---

## Шаг 5 — Мониторинг

Статус сервиса:

```bash
sudo systemctl status armybots
```

Живые логи:

```bash
sudo journalctl -u armybots -f
```

Последние 200 строк:

```bash
sudo journalctl -u armybots -n 200 --no-pager
```

Контейнеры БД:

```bash
docker compose ps
docker compose logs -f postgres
```

Caddy / HTTPS (после Шага 4):

```bash
sudo journalctl -u caddy -f
sudo tail -f /var/log/caddy/armybots.log
```

---

## Обновление до новой версии

```bash
cd ~/army-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart armybots
sudo journalctl -u armybots -n 50 --no-pager
```
