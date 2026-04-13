#!/usr/bin/env bash
#
# Bot Factory — initial server bootstrap for Ubuntu 22.04 LTS.
# Automates steps 1-2 from deployment/README.md on a clean Selectel VM.
#
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/<org>/army-bot/main/deployment/setup.sh | bash
# or:
#   wget https://raw.githubusercontent.com/<org>/army-bot/main/deployment/setup.sh
#   chmod +x setup.sh && ./setup.sh
#
# After the script finishes:
#   1. switch to the `deploy` user (su - deploy)
#   2. edit /home/deploy/army-bot/.env
#   3. run `docker compose up -d && alembic upgrade head`
#   4. follow step 3+ from deployment/README.md (systemd, Caddy)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/pohilkom89-ctrl/army-bot.git}"
DEPLOY_USER="deploy"
APP_DIR="/home/${DEPLOY_USER}/army-bot"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This script must be run as root."
[[ -f /etc/os-release ]] || die "Cannot detect OS."
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || warn "Tested on Ubuntu 22.04, detected: $PRETTY_NAME"

# -------- STEP 1.1: system upgrade --------
log "Updating apt cache and upgrading system packages"
export DEBIAN_FRONTEND=noninteractive
apt update
apt upgrade -y

# -------- STEP 1.2: base utilities --------
log "Installing base utilities"
apt install -y \
    curl git ufw ca-certificates gnupg lsb-release \
    python3 python3-venv python3-pip

# -------- STEP 1.3: Docker + compose plugin --------
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker CE + compose plugin"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt update
    apt install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    log "Docker already installed, skipping"
fi

# -------- STEP 1.4: deploy user --------
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "Creating user '$DEPLOY_USER'"
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
    usermod -aG docker "$DEPLOY_USER"
    usermod -aG sudo "$DEPLOY_USER"

    if [[ -f /root/.ssh/authorized_keys ]]; then
        log "Copying root SSH keys to $DEPLOY_USER"
        install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/${DEPLOY_USER}/.ssh"
        install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
            /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/authorized_keys"
    else
        warn "No /root/.ssh/authorized_keys found — set a password or add keys manually for $DEPLOY_USER"
    fi
else
    log "User '$DEPLOY_USER' already exists, skipping"
fi

# -------- STEP 1.5: UFW firewall --------
log "Configuring UFW firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    comment 'SSH'
ufw allow 80/tcp    comment 'HTTP (Caddy ACME)'
ufw allow 443/tcp   comment 'HTTPS (YooKassa webhook)'
ufw allow 8080/tcp  comment 'App backend'
ufw --force enable
ufw status verbose

# -------- STEP 2.1: clone repo as deploy user --------
if [[ ! -d "$APP_DIR/.git" ]]; then
    log "Cloning $REPO_URL into $APP_DIR"
    sudo -u "$DEPLOY_USER" git clone "$REPO_URL" "$APP_DIR"
else
    log "Repo already cloned at $APP_DIR, pulling latest"
    sudo -u "$DEPLOY_USER" git -C "$APP_DIR" pull --ff-only
fi

# -------- STEP 2.2: .env bootstrap --------
if [[ ! -f "$APP_DIR/.env" ]]; then
    log "Creating .env from .env.example (needs manual editing!)"
    sudo -u "$DEPLOY_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    warn "Edit $APP_DIR/.env before continuing: nano $APP_DIR/.env"
else
    log ".env already present, leaving untouched"
fi

# -------- STEP 2.3: Python venv + deps --------
if [[ ! -d "$APP_DIR/.venv" ]]; then
    log "Creating Python virtualenv and installing requirements"
    sudo -u "$DEPLOY_USER" python3 -m venv "$APP_DIR/.venv"
    sudo -u "$DEPLOY_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
    sudo -u "$DEPLOY_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
else
    log "Virtualenv already exists at $APP_DIR/.venv"
fi

# -------- Done --------
cat <<EOF

========================================================================
  Bootstrap finished. Next manual steps:

  1. Switch to deploy user:
       su - $DEPLOY_USER

  2. Fill secrets in the env file:
       nano $APP_DIR/.env

  3. Start Postgres + Redis:
       cd $APP_DIR && docker compose up -d

  4. Run migrations:
       source .venv/bin/activate && alembic upgrade head

  5. Continue with steps 3-4 from deployment/README.md
     (systemd unit + Caddy + Let's Encrypt).
========================================================================

EOF
