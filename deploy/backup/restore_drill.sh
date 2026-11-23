#!/bin/sh
# Restore drill: restore the newest (or given) dump into a SCRATCH database, check it, drop it.
# Never touches the live database. Env as backup.sh; DRILL_DB (default tempo_restore_drill).
set -eu
: "${BACKUP_DIR:=/backups}" "${DRILL_DB:=tempo_restore_drill}"
if [ -n "${PGPASSWORD_FILE:-}" ]; then PGPASSWORD="$(cat "$PGPASSWORD_FILE")"; export PGPASSWORD; fi
dump="${1:-$(ls -1t "$BACKUP_DIR"/tempo-*.dump | head -n 1)}"
[ "$DRILL_DB" != "$PGDATABASE" ] || { echo "refusing: drill database equals live database"; exit 2; }
dropdb --if-exists "$DRILL_DB"
createdb "$DRILL_DB"
pg_restore --no-owner --exit-on-error --dbname="$DRILL_DB" "$dump"
version="$(psql -At -d "$DRILL_DB" -c 'SELECT version_num FROM alembic_version')"
for table in study_task fixed_event schedule_version study_block activity audit_entry; do
  live="$(psql -At -d "$PGDATABASE" -c "SELECT count(*) FROM $table")"
  restored="$(psql -At -d "$DRILL_DB" -c "SELECT count(*) FROM $table")"
  echo "$table: live=$live restored=$restored"
done
dropdb "$DRILL_DB"
echo "restore drill ok: $dump (schema $version)"
