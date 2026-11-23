#!/bin/sh
# Tempo routing supervisor (milestone 13): serves MOTIS from /data/current and rebuilds the graph
# when the input updater (backend: python -m tempo.transport.gtfs_update /input) publishes a new
# version in /input/READY. No Docker socket needed.
#
#   build   : motis import -> /data/build-<version>; success requires exit 0 and tt.bin
#   switch  : atomic symlink rename current -> build-<version>, then server restart (~1-2 min)
#   failure : the current graph keeps serving; the version is recorded in /data/FAILED and not
#             retried until a newer version appears; import log kept in /data/import-<v>.log
#   prune   : keeps the current graph and the previous one (rollback: relink `current`)
set -u
DATA="${DATA:-/data}"
INPUT="${INPUT:-/input}"
CONFIG="${CONFIG:-/config/config.yml}"
MOTIS="${MOTIS:-/opt/motis/motis}"
CHECK_SECONDS="${CHECK_SECONDS:-300}"
PID=""

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor: $*"; }

read_file() { [ -f "$1" ] && cat "$1" || echo ""; }

valid_version() { echo "$1" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$'; }

prune() {
  current_target="$(readlink "$DATA/current" 2>/dev/null || echo "")"
  # newest first; keep the current target and the newest other build
  kept_other=""
  for dir in $(ls -1d "$DATA"/build-* 2>/dev/null | sort -r); do
    name="$(basename "$dir")"
    if [ "$name" = "$current_target" ]; then continue; fi
    if [ -z "$kept_other" ]; then kept_other="$name"; continue; fi
    log "pruning old graph $name"
    rm -rf -- "$DATA/$name"
  done
}

build() {
  version="$1"
  valid_version "$version" || { log "ignoring malformed version '$version'"; return 1; }
  dir="$DATA/build-$version"
  rm -rf -- "$dir"
  log "importing version $version"
  if "$MOTIS" import -c "$CONFIG" -d "$dir" > "$DATA/import-$version.log" 2>&1 && [ -f "$dir/tt.bin" ]; then
    echo "$version" > "$dir/.version"
    ln -sfn "build-$version" "$DATA/current.new"
    mv -T "$DATA/current.new" "$DATA/current"
    rm -f "$DATA/FAILED"
    log "switched to version $version"
    prune
    return 0
  fi
  log "import of version $version FAILED; keeping the current graph"
  echo "$version" > "$DATA/FAILED"
  rm -rf -- "$dir"
  return 1
}

start_server() {
  "$MOTIS" server -d "$DATA/current" &
  PID=$!
  log "server started (pid $PID) on version $(read_file "$DATA/current/.version")"
}

stop_server() {
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null
    wait "$PID" 2>/dev/null
  fi
  PID=""
}

trap 'log "stopping"; stop_server; exit 0' TERM INT

while [ ! -e "$DATA/current" ]; do
  ready="$(read_file "$INPUT/READY")"
  if [ -n "$ready" ] && [ "$ready" != "$(read_file "$DATA/FAILED")" ] && build "$ready"; then
    break
  fi
  log "waiting for valid routing inputs ($INPUT/READY)"
  sleep "$CHECK_SECONDS" & wait $!
done

start_server
while true; do
  sleep "$CHECK_SECONDS" & wait $!
  if ! kill -0 "$PID" 2>/dev/null; then
    log "server exited unexpectedly; restarting"
    start_server
    continue
  fi
  ready="$(read_file "$INPUT/READY")"
  current="$(read_file "$DATA/current/.version")"
  failed="$(read_file "$DATA/FAILED")"
  if [ -n "$ready" ] && [ "$ready" != "$current" ] && [ "$ready" != "$failed" ]; then
    if build "$ready"; then
      stop_server
      start_server
    fi
  fi
done
