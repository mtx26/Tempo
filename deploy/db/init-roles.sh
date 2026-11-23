#!/bin/sh
# First-initialisation script for the PostgreSQL container (/docker-entrypoint-initdb.d).
# POSTGRES_USER (owner, used ONLY by the one-shot migration job) owns the schema.
# tempo_app is the runtime role (api + worker): DML only, granted by
# `python -m tempo.db grant-runtime tempo_app` after every migration.
set -eu
app_password="$(cat /run/secrets/tempo_app_db_password)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$app_password" <<'SQL'
CREATE ROLE tempo_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
REVOKE ALL ON DATABASE tempo FROM PUBLIC;
GRANT CONNECT ON DATABASE tempo TO tempo_app;
SQL
