#!/bin/sh
# Tempo routing supervisor: serves MOTIS from /data/current and rebuilds the graph
# when the input updater (backend: python -m tempo.transport.gtfs_update /input) publishes a new
# version in /input/READY, or when the MOTIS configuration changed (MOTIS reads the configuration
# copied at import time, e.g. enabling geocoding needs a new import). No Docker socket needed.
#
#   build   : import the immutable snapshot, start an isolated server and check routing
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
PROBE_PID=""

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
  snapshot="$INPUT/snapshots/$version"
  [ "$name" = "$(readlink "$DATA/current" 2>/dev/null || echo "")" ] && return 1  # never the live graph
  rm -rf -- "$dir"
  report="$DATA/import-$version.log"
  tags="$(sed -n 's|^      path: /input/\([a-z]*\)\.gtfs\.zip$|\1|p' "$CONFIG")"
  if [ ! -s "$snapshot/osm.pbf" ] || [ -z "$tags" ]; then
    log "missing OSM input or GTFS datasets for $version"
    return 1
  fi
  for tag in $tags; do
    if [ ! -s "$snapshot/$tag.gtfs.zip" ]; then
      log "missing $tag GTFS in snapshot $version"
      return 1
    fi
  done
  candidate_config="$DATA/candidate.yml"
  sed "s|/input/|$snapshot/|g" "$CONFIG" > "$candidate_config"
  log "importing snapshot $version (configuration $cfg)"
  if "$MOTIS" import -c "$candidate_config" -d "$dir" > "$report" 2>&1 &&
    [ -s "$dir/tt.bin" ] && check_candidate "$dir" "$tags" >> "$report" 2>&1; then
    echo "$version" > "$dir/.version"
    echo "$cfg" > "$dir/.config"
    return 0
  fi
  log "snapshot $version import or candidate check failed; see $report"
  echo "$version-$cfg" > "$DATA/FAILED"
  rm -rf -- "$dir"
  return 1
}

wait_for_health() {
  for attempt in $(seq 1 60); do
    kill -0 "$2" 2>/dev/null || return 1
    if health="$(curl -fsS --max-time 2 "$1/api/v1/health" 2>/dev/null)" &&
      [ -n "$health" ] && printf '%s\n' "$health" |
      jq -e '(.rt | type == "boolean") and (.gbfs | type == "boolean")' >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

check_candidate() {
  candidate="$1"
  # The MOTIS server reads its imported config. Probe on loopback alongside the active server.
  cp "$candidate/config.yml" "$candidate/config.production.yml" || return 1
  sed -e '/^  host:/d' -e 's/^  port: .*$/  host: 127.0.0.1\n  port: 18081/' \
    "$candidate/config.production.yml" > "$candidate/config.yml"
  "$MOTIS" server -d "$candidate" &
  PROBE_PID=$!
  probe=http://127.0.0.1:18081
  tomorrow="$(date -u -d tomorrow +%Y-%m-%d)T10:00:00Z"
  ok=false
  if wait_for_health "$probe" "$PROBE_PID" &&
    map="$(curl -fsS --max-time 15 "$probe/api/v1/map/initial")" &&
    [ -n "$map" ] && printf '%s\n' "$map" |
      jq -e '.serverConfig.hasStreetRouting == true' >/dev/null &&
    route="$(curl -fsS --max-time 15 \
      "$probe/api/v6/plan?fromPlace=50.8363,4.3366&toPlace=50.8454,4.3571&time=$tomorrow&transitModes=RAIL&realtimeMode=OFF")" &&
    [ -n "$route" ] && printf '%s\n' "$route" |
      jq -e 'any(.itineraries[].legs[]; (.tripId // "") | contains("_sncb_"))' >/dev/null; then
    ok=true
    if stops="$(curl -fsS --max-time 15 "$probe/api/v6/map/stops?min=50.81,4.29&max=50.92,4.48")" &&
      [ -n "$stops" ]; then
      for tag in $2; do
        if ! printf '%s' "$stops" |
          jq -e --arg tag "${tag}_" 'any(.[]; .stopId | startswith($tag))' >/dev/null; then
          log "candidate has no reference stops for $tag"
          ok=false
        fi
      done
    else
      ok=false
    fi
  else
    log "candidate MOTIS failed health, street routing or the reference train journey"
  fi
  kill -TERM "$PROBE_PID" 2>/dev/null || :
  wait "$PROBE_PID" 2>/dev/null || :
  PROBE_PID=""
  mv -f "$candidate/config.production.yml" "$candidate/config.yml" || return 1
  [ "$ok" = true ]
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

activate() {
  old="$(readlink "$DATA/current" 2>/dev/null || echo "")"
  stop_server
  if ! (ln -sfn "$name" "$DATA/current.new" && mv -T "$DATA/current.new" "$DATA/current"); then
    [ -z "$old" ] || start_server
    return 1
  fi
  start_server
  if wait_for_health http://127.0.0.1:8080 "$PID"; then
    rm -f "$DATA/FAILED"
    log "switched to $name"
    prune
    return 0
  fi
  log "live start failed for $name; restoring $old"
  stop_server
  echo "$version-$cfg" > "$DATA/FAILED"
  if [ -n "$old" ]; then
    ln -sfn "$old" "$DATA/current.new" && mv -T "$DATA/current.new" "$DATA/current"
    start_server
  else
    rm -f "$DATA/current"
  fi
  return 1
}

trap 'log "stopping"; [ -z "$PROBE_PID" ] || kill -TERM "$PROBE_PID" 2>/dev/null; stop_server; exit 0' TERM INT

while [ ! -e "$DATA/current" ]; do
  ready="$(read_file "$INPUT/READY")"
  if [ -n "$ready" ] && [ "$ready-$(config_hash)" != "$(read_file "$DATA/FAILED")" ] &&
    build "$ready" && activate; then
    break
  fi
  log "waiting for valid routing inputs ($INPUT/READY)"
  sleep "$CHECK_SECONDS" & wait $!
done

[ -n "$PID" ] || start_server
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
    if build "$ready"; then activate || :; fi
  fi
done
