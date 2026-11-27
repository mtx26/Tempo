#!/bin/sh
# Run inside the Tempo routing image with tempo-routing-brussels at /reference (read-only).
set -eu

work="$(mktemp -d)"
export DATA="$work/data" INPUT="$work/input" MOTIS="$work/motis" CHECK_SECONDS=1
export CONFIG=/test-config.yml
mkdir -p "$DATA" "$INPUT"
ln -s /reference/brussels "$DATA/current"

# A successful import can still produce a graph its server cannot load.
cat > "$MOTIS" <<'SCRIPT'
#!/bin/sh
set -eu
if [ "$1" = import ]; then
  mkdir -p "$5"
  case "$5" in
    *20261127T010000Z*)
      cp /reference/brussels/config.yml "$5/config.yml"
      printf 'not a timetable' > "$5/tt.bin"
      ;;
    *)
      cp -a /reference/brussels/. "$5/"
      ;;
  esac
else
  exec /opt/motis/motis "$@"
fi
SCRIPT
chmod +x "$MOTIS"

publish() {
  mkdir -p "$INPUT/snapshots/$1"
  for source in osm.pbf stib.gtfs.zip sncb.gtfs.zip; do
    printf 'test input' > "$INPUT/snapshots/$1/$source"
  done
  printf '%s' "$1" > "$INPUT/READY"
}

publish 20261127T010000Z
/opt/tempo/supervisor.sh > "$work/log" 2>&1 &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null || :; wait "$pid" || :; cat "$work/log"' EXIT

for i in $(seq 1 20); do
  [ -s "$DATA/FAILED" ] && break
  sleep 1
done
grep -q '^20261127T010000Z-' "$DATA/FAILED"
[ "$(readlink "$DATA/current")" = /reference/brussels ]
for i in $(seq 1 15); do
  curl -fsS --max-time 2 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1:8080/api/v1/health >/dev/null

publish 20261127T020000Z
for i in $(seq 1 90); do
  grep -q 'switched to build-20261127T020000Z-' "$work/log" && exit 0
  sleep 1
done
test ! -f "$DATA/import-20261127T020000Z.log" || cat "$DATA/import-20261127T020000Z.log"
exit 1
