#!/usr/bin/env bash
# Install + configure Caddy on the prod VPS.
#
# Run on prod as root:
#   ssh root@45.12.73.106 'bash -s' < deployment/install_caddy.sh
# or after scp'ing this file + Caddyfile + landing/:
#   ssh root@45.12.73.106 'bash /tmp/install_caddy.sh'
#
# What it does (idempotent — safe to re-run):
#   1. Adds Caddy's official apt repo (skips if already there)
#   2. apt install caddy
#   3. Copies Caddyfile (default: /etc/caddy/Caddyfile)
#   4. Copies landing page to /var/www/armybots/
#   5. Enables + starts caddy
#   6. systemctl status + curl smoke check
#
# Pre-reqs (verify BEFORE running):
#   - DNS for armybots.ru / www.armybots.ru must resolve to this VPS via
#     PUBLIC resolvers (Google 8.8.8.8 + Cloudflare 1.1.1.1). Without
#     this Let's Encrypt ACME-challenge fails and you can hit the
#     5-fails-per-hour rate limit. Test:
#       nslookup armybots.ru 8.8.8.8
#       nslookup armybots.ru 1.1.1.1
#   - Ports 80 and 443 must be open in UFW (already done per
#     deployment/README.md Step 1).
#   - Nothing else listening on 80/443:
#       ss -tlnp | grep -E ':80 |:443 '
#
# After successful install:
#   - Verify HTTPS:
#       curl -I https://armybots.ru
#       curl -i https://armybots.ru/webhook/yukassa  (expect 405 from aiohttp)
#   - Close 8080 to the public internet:
#       ufw delete allow 8080/tcp
#       ufw status
#   - Update YooKassa cabinet webhook URL to https://armybots.ru/webhook/yukassa

set -euo pipefail

CADDYFILE_SRC="${CADDYFILE_SRC:-/tmp/Caddyfile}"
LANDING_SRC="${LANDING_SRC:-/tmp/armybots-landing}"

echo "=== 1. Add Caddy apt repo (idempotent) ==="
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
    apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt update
else
    echo "Caddy apt repo already configured."
fi

echo "=== 2. apt install caddy ==="
apt install -y caddy

echo "=== 3. Install Caddyfile ==="
if [ ! -f "$CADDYFILE_SRC" ]; then
    echo "ERROR: $CADDYFILE_SRC not found. scp deployment/Caddyfile to /tmp/Caddyfile first."
    exit 1
fi
install -m 0644 -o root -g root "$CADDYFILE_SRC" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

echo "=== 4. Install landing page ==="
mkdir -p /var/www/armybots
if [ -d "$LANDING_SRC" ]; then
    cp -r "$LANDING_SRC"/* /var/www/armybots/
else
    echo "WARNING: $LANDING_SRC not found — landing won't be served."
    echo "scp -r deployment/landing/* root@host:/var/www/armybots/"
fi
chown -R caddy:caddy /var/www/armybots
chmod -R 755 /var/www/armybots
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy

echo "=== 5. Enable + start caddy ==="
systemctl enable caddy
systemctl restart caddy
sleep 3
systemctl is-active caddy

echo "=== 6. Smoke checks ==="
echo "--- caddy status (truncated) ---"
systemctl status caddy --no-pager -n 15 || true
echo
echo "--- Local http on :80 (Caddy serves ACME challenge here) ---"
curl -sI -o /dev/null -w "http://localhost/ → %{http_code}\n" --max-time 5 http://localhost/ || echo "local 80 unreachable"
echo
echo "--- Public HTTPS test (will fail until cert is issued ~30-60s after first start) ---"
curl -sI -o /dev/null -w "https://armybots.ru/ → %{http_code}\n" --max-time 15 https://armybots.ru/ || \
    echo "HTTPS not ready yet — wait 60s, check 'journalctl -u caddy -n 50' for cert status"
echo
echo "=== Done. Remember: ==="
echo "  - Wait ~60s for Let's Encrypt cert acquisition on first run"
echo "  - Check cert: journalctl -u caddy -n 100 | grep -i certificate"
echo "  - When HTTPS confirmed working: ufw delete allow 8080/tcp"
echo "  - Update YooKassa webhook URL to https://armybots.ru/webhook/yukassa"
