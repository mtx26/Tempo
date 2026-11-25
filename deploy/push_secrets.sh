#!/bin/sh
# Create the ten Swarm secrets of the Tempo stack on the server, over SSH.
#   sh deploy/push_secrets.sh <ssh-host>        (e.g. sh deploy/push_secrets.sh home)
# Values come from deploy/secrets/swarm/ (python deploy/make_swarm_secrets.py) and travel only
# through SSH stdin: never printed, never in a command line. Existing secrets are kept (Swarm
# secrets are immutable: delete one first to replace it).
set -eu
HOST=${1:?usage: sh deploy/push_secrets.sh <ssh-host>}
DIR=$(dirname "$0")/secrets/swarm
NAMES="tempo_owner_db_password tempo_app_db_password tempo_owner_database_url tempo_database_url
tempo_api_token tempo_calendar_token tempo_ics_url tempo_bmc_partner_key tempo_gcp_credentials tempo_training_imap_password"
for name in $NAMES; do
  [ -s "$DIR/$name" ] || { echo "missing $DIR/$name: run python deploy/make_swarm_secrets.py"; exit 1; }
done
for name in $NAMES; do
  if ssh "$HOST" docker secret inspect "$name" >/dev/null 2>&1; then
    echo "kept    $name (already on the server)"
  else
    ssh "$HOST" docker secret create "$name" - <"$DIR/$name" >/dev/null
    echo "created $name"
  fi
done
echo "secrets on $HOST: $(ssh "$HOST" docker secret ls --format '{{.Name}}' | grep -c '^tempo_')/10"
