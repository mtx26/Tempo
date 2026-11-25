# Tempo PostgreSQL image: official postgres 16 (pinned digest) + Tempo's role initialisation and
# backup scripts, so the Swarm stack needs no file from the repository (Portainer web editor).
#   docker build -f deploy/postgres.Dockerfile -t tempo-postgres:dev deploy
FROM postgres:16-alpine@sha256:f2f98726820d0342055351c6e9d36e705f5bfffb3dd53ff556325114b08afb6d
COPY --chmod=0555 db/init-roles.sh /docker-entrypoint-initdb.d/10-roles.sh
COPY --chmod=0555 backup/backup.sh backup/restore_drill.sh /scripts/
