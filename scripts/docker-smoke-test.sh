#!/usr/bin/env bash
# Ende-zu-Ende-Test des fertigen Images mit SCHREIBGESCHÜTZTEM Medienverzeichnis (Abnahmekriterium G).
# Erzeugt Testmedien, startet den Container mit /media:ro, führt einen Vollscan aus und prüft,
# dass keine Mediendatei verändert wurde.
#   scripts/docker-smoke-test.sh [image]
set -euo pipefail
IMAGE="${1:-media-integrity-scanner:latest}"
WORK="$(mktemp -d)"
NAME="mis-smoke-$$"
PORT="${PORT:-18080}"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/media/Filme" "$WORK/config"
# Testmedien im Container erzeugen (ffmpeg ist dort vorhanden)
docker run --rm --entrypoint sh -v "$WORK/media/Filme:/out" "$IMAGE" -c "
  ffmpeg -nostdin -loglevel error -y -f lavfi -i testsrc2=size=320x240:rate=25 -f lavfi -i sine=frequency=440 -t 10 -c:v libx264 -preset veryfast -g 50 -c:a aac '/out/Gut – Ä.mkv' &&
  head -c 400000 '/out/Gut – Ä.mkv' > /out/abgeschnitten.mkv && chmod -R a+rX /out"
( cd "$WORK/media" && find . -type f -exec sha256sum {} + | sort ) > "$WORK/before.txt"

docker run -d --name "$NAME" -p "$PORT:8080" -e PUID="$(id -u)" -e PGID="$(id -g)" \
  -v "$WORK/media:/media:ro" -v "$WORK/config:/config" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/api/system" | grep -q '"media_writable":\[\]' && echo "OK: /media ist schreibgeschützt eingebunden"
JOB=$(curl -fs -XPOST "http://127.0.0.1:$PORT/api/jobs" -H 'content-type: application/json' \
  -d '{"task":"incremental","methods":["quick","container","full"]}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
for _ in $(seq 1 120); do
  STATE=$(curl -fs "http://127.0.0.1:$PORT/api/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$STATE" = completed ] && break; sleep 1
done
echo "Auftrag $JOB: $STATE"
curl -fs "http://127.0.0.1:$PORT/api/files" | python3 -c '
import json,sys
items=json.load(sys.stdin)["items"]
for f in items: print("  %-10s %-15s %s" % (f["status"], f["check_level"], f["filename"]))
st={f["filename"]:f["status"] for f in items}
assert st["Gut – Ä.mkv"]=="ok", st
assert st["abgeschnitten.mkv"]=="critical", st'
( cd "$WORK/media" && find . -type f -exec sha256sum {} + | sort ) > "$WORK/after.txt"
diff "$WORK/before.txt" "$WORK/after.txt" && echo "OK: Mediendateien unverändert"
docker inspect --format '{{.State.Health.Status}}' "$NAME" || true
echo "Smoke-Test erfolgreich."
