#!/bin/sh
# Tempo PostgreSQL backup (runs in a postgres client container; see docs/DEPLOYMENT.md).
# Env: PGHOST PGPORT PGUSER PGDATABASE, PGPASSWORD_FILE (Docker secret), BACKUP_DIR,
#      BACKUP_KEEP_DAYS (default 14). Writes custom-format dumps and verifies each one.
set -eu
: "${BACKUP_DIR:=/backups}" "${BACKUP_KEEP_DAYS:=14}"
if [ -n "${PGPASSWORD_FILE:-}" ]; then PGPASSWORD="$(cat "$PGPASSWORD_FILE")"; export PGPASSWORD; fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/tempo-$stamp.dump"
umask 077
pg_dump --format=custom --no-owner --file="$target.part" "$PGDATABASE"
pg_restore --list "$target.part" > /dev/null   # the archive must be readable
mv "$target.part" "$target"
find "$BACKUP_DIR" -name 'tempo-*.dump' -type f -mtime +"$BACKUP_KEEP_DAYS" -delete
echo "backup ok: $target ($(wc -c < "$target") bytes)"
