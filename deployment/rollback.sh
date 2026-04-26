#!/usr/bin/env bash
# Emergency rollback: switch prod to a known-good commit and restart.
#
# Usage:
#   ./rollback.sh                    Show last 10 commits, exit 0
#   ./rollback.sh <commit_or_tag>    Checkout, install deps, migrate, restart
#   ./rollback.sh --abort            Release the lock if a previous run died
#
# Steps when invoked with a target:
#   1. Acquire /tmp/armybots.lock (flock) — no parallel rollback/deploy.
#   2. git fetch + git checkout <target>.
#   3. pip install -r requirements.txt if requirements.txt changed.
#   4. alembic upgrade head if alembic/versions/ changed (no auto-downgrade —
#      downgrades require operator awareness, see README).
#   5. sudo systemctl restart armybots.
#   6. Verify /health returns 200 within 30 seconds.
#   7. Log the rollback to /var/log/armybots-rollback.log.
#
# Designed to run as the `deploy` user. systemctl restart works because
# /etc/sudoers.d/deploy-armybots permits it without password.

set -euo pipefail

REPO=/home/deploy/army-bot
LOCK=/tmp/armybots.lock
LOG=/var/log/armybots-rollback.log
HEALTH_URL=http://127.0.0.1:8080/health

cd "$REPO"

if [ "${1:-}" = "--abort" ]; then
    rm -f "$LOCK"
    echo "lock released"
    exit 0
fi

if [ -z "${1:-}" ]; then
    echo "=== last 10 commits on $(git rev-parse --abbrev-ref HEAD) ==="
    git log --oneline -10
    echo
    echo "Usage: $0 <commit_or_tag>   to roll back"
    exit 0
fi

TARGET=$1

# Acquire exclusive lock — refuse if another deploy is in flight.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "ERROR: another deploy/rollback is in progress (lock $LOCK)"
    echo "If you're sure none is running, run: $0 --abort"
    exit 2
fi

CURRENT=$(git rev-parse --short HEAD)
TS=$(date -u +%FT%TZ)
echo "[$TS] rollback start: $CURRENT -> $TARGET" | tee -a "$LOG"

# Resolve target — support short hash, full hash, tag, branch.
git fetch --quiet origin
if ! git rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null; then
    echo "ERROR: $TARGET is not a known commit/tag/branch" | tee -a "$LOG"
    exit 3
fi
TARGET_FULL=$(git rev-parse "$TARGET")

# Detect what changed between current and target so we know which
# follow-ups to run.
REQS_CHANGED=$(git diff --name-only "$CURRENT" "$TARGET_FULL" -- requirements.txt | wc -l)
ALEMBIC_CHANGED=$(git diff --name-only "$CURRENT" "$TARGET_FULL" -- alembic/ | wc -l)

git checkout "$TARGET_FULL" --quiet
echo "[$TS] checked out $TARGET_FULL" | tee -a "$LOG"

if [ "$REQS_CHANGED" -gt 0 ]; then
    echo "[$TS] requirements.txt changed — pip install" | tee -a "$LOG"
    .venv/bin/pip install -r requirements.txt --quiet
fi

if [ "$ALEMBIC_CHANGED" -gt 0 ]; then
    echo "[$TS] alembic/ changed — running upgrade head" | tee -a "$LOG"
    set -a; . ./.env; set +a
    .venv/bin/alembic upgrade head
fi

echo "[$TS] systemctl restart armybots" | tee -a "$LOG"
sudo -n systemctl restart armybots

# Verify /health responds 200 within 30s — gives the service time to bind
# the port and run init_db.
echo "[$TS] verifying /health" | tee -a "$LOG"
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$HEALTH_URL" || echo "000")
    if [ "$code" = "200" ]; then
        echo "[$TS] /health → 200 after ${i}s — rollback OK" | tee -a "$LOG"
        exit 0
    fi
    sleep 1
done

echo "[$TS] FAIL: /health did not return 200 within 30s" | tee -a "$LOG"
echo "Service status:"
systemctl status armybots --no-pager -n 20 || true
exit 4
