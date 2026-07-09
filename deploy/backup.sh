#!/bin/bash
# Nightly SQLite backup. Add to root crontab:
#   0 2 * * * /opt/fundscan/deploy/backup.sh >> /var/log/fundscan-backup.log 2>&1

set -euo pipefail

DB=/var/lib/fundscan/fundscan.db
BACKUP_DIR=/var/backups/fundscan
DATE=$(date +%Y-%m-%d)
DEST="$BACKUP_DIR/fundscan-$DATE.db"

mkdir -p "$BACKUP_DIR"

# sqlite3 .backup is safe against concurrent writes (uses WAL checkpoint)
sqlite3 "$DB" ".backup '$DEST'"
gzip -f "$DEST"

# Keep last 30 days only
find "$BACKUP_DIR" -name "*.db.gz" -mtime +30 -delete

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Backup complete: $DEST.gz"
