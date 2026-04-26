#!/usr/bin/env bash
# Daily Postgres dump for Bot Factory.
# Runs from cron at 03:00 Europe/Moscow (00:00 UTC) — low-traffic window.
# pg_dump streamed through gzip into /home/deploy/backups/YYYY-MM-DD.sql.gz,
# then anything older than RETENTION_DAYS is pruned.
#
# Install (as root):
#   chmod +x /home/deploy/army-bot/backup/dump.sh
#   touch /var/log/armybots-backup.log
#   chown deploy:deploy /var/log/armybots-backup.log
#   echo "0 0 * * * deploy /home/deploy/army-bot/backup/dump.sh" \
#       > /etc/cron.d/armybots-backup
#   chmod 644 /etc/cron.d/armybots-backup
#
# Manual run for testing:
#   sudo -u deploy /home/deploy/army-bot/backup/dump.sh

set -euo pipefail

BACKUP_DIR=/home/deploy/backups
LOG=/var/log/armybots-backup.log
RETENTION_DAYS=7
ENV_FILE=/home/deploy/army-bot/.env
CONTAINER=botfactory_postgres

DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%FT%TZ)
DUMPFILE="$BACKUP_DIR/$DATE.sql.gz"

mkdir -p "$BACKUP_DIR"

# Parse Postgres credentials from DATABASE_URL.
DSN=$(grep ^DATABASE_URL= "$ENV_FILE" | sed "s/^DATABASE_URL=//")
PGPASS=$(echo "$DSN" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$DSN" | sed -E 's|.*://([^:]+):.*|\1|')
PGDB=$(echo "$DSN" | sed -E 's|.*/([^?]+).*|\1|')

START=$(date -u +%s)

# Write to .tmp first; rename only on success — partial dumps never become
# the file an operator might restore from.
if docker exec -e PGPASSWORD="$PGPASS" "$CONTAINER" \
        pg_dump -U "$PGUSER" --no-owner --no-privileges "$PGDB" \
        | gzip -9 > "$DUMPFILE.tmp"; then
    mv "$DUMPFILE.tmp" "$DUMPFILE"
    SIZE=$(du -h "$DUMPFILE" | awk '{print $1}')
    DURATION=$(( $(date -u +%s) - START ))
    echo "[$TS] OK $DUMPFILE ($SIZE in ${DURATION}s)" >> "$LOG"
else
    rm -f "$DUMPFILE.tmp"
    echo "[$TS] FAIL pg_dump returned non-zero" >> "$LOG"
    exit 1
fi

# Retention: drop dumps older than RETENTION_DAYS. Wrapped in || true
# so a stale pipe (no files to delete + set -o pipefail interaction)
# doesn't fail the script after a successful dump.
PRUNED=$(find "$BACKUP_DIR" -maxdepth 1 -name "*.sql.gz" -mtime +"$RETENTION_DAYS" -print -delete 2>/dev/null || true)
if [ -n "$PRUNED" ]; then
    while read -r f; do
        [ -z "$f" ] && continue
        echo "[$TS] PRUNED $f" >> "$LOG"
    done <<< "$PRUNED"
fi

exit 0
