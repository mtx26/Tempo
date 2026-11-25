#!/bin/sh
# Tempo routing supervisor: serves MOTIS from /data/current and rebuilds the graph
# when the input updater (backend: python -m tempo.transport.gtfs_update /input) publishes a new
# version in /input/READY, or when the MOTIS configuration changed (MOTIS reads the configuration
# copied at import time, e.g. enabling geocoding needs a new import). No Docker socket needed.
#
#   build   : motis import -> /data/build-<version>-<config hash>; success requires exit 0 and
#             tt.bin
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

config_hash() { sha256sum "$CONFIG" | cut -c1-12; }

# Strict single-token check (a multi-line READY must never reach a path such as rm -rf).
valid_version() {
  [ "${#1}" -eq 16 ] || return 1
  case "$1" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) return 0 ;;
    *) return 1 ;;
  esac
}

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
  # keep the 5 most recent import logs
  ls -1t "$DATA"/import-*.log 2>/dev/null | tail -n +6 | while read -r old_log; do rm -f -- "$old_log"; done
}

build() {
  version="$1"
  valid_version "$version" || { log "ignoring malformed version '$version'"; return 1; }
  cfg="$(config_hash)"
  name="build-$version-$cfg"
  dir="$DATA/$name"
  [ "$name" = "$(readlink "$DATA/current" 2>/dev/null || echo "")" ] && return 1  # never the live graph
  rm -rf -- "$dir"
  log "importing version $version (configuration $cfg)"
  if "$MOTIS" import -c "$CONFIG" -d "$dir" > "$DATA/import-$version.log" 2>&1 && [ -f "$dir/tt.bin" ]; then
    echo "$version" > "$dir/.version"
    echo "$cfg" > "$dir/.config"
    ln -sfn "$name" "$DATA/current.new"
    mv -T "$DATA/current.new" "$DATA/current"
    rm -f "$DATA/FAILED"
    log "switched to version $version (configuration $cfg)"
    prune
    return 0
  fi
  log "import of version $version FAILED; keeping the current graph"
  echo "$version-$cfg" > "$DATA/FAILED"
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
  if [ -n "$ready" ] && [ "$ready-$(config_hash)" != "$(read_file "$DATA/FAILED")" ] && build "$ready"; then
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
  current="$(read_file "$DATA/current/.version")-$(read_file "$DATA/current/.config")"
  wanted="$ready-$(config_hash)"
  failed="$(read_file "$DATA/FAILED")"
  if [ -n "$ready" ] && [ "$wanted" != "$current" ] && [ "$wanted" != "$failed" ]; then
    if build "$ready"; then
      stop_server
      start_server
    fi
  fi
done
