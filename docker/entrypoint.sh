#!/bin/sh
# Startskript: setzt PUID/PGID um und startet die Anwendung ohne Root-Rechte.
#
# - Läuft der Container als root (Standard), wird /config dem Zielbenutzer übergeben
#   und der Prozess per setpriv mit PUID/PGID (+ optional SUPPLEMENTARY_GROUPS) gestartet.
# - Läuft der Container bereits als Nicht-root (z. B. "user:" in Compose), wird direkt gestartet.
# - Das Medienverzeichnis wird NIE verändert (kein chown/chmod auf /media).
set -eu

PUID="${PUID:-3000}"
PGID="${PGID:-3000}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
PORT="${PORT:-8080}"

case "$PUID$PGID" in *[!0-9]*) echo "PUID/PGID müssen Zahlen sein" >&2; exit 1;; esac

# Port aus der Umgebung an uvicorn übergeben
if [ "${1:-}" = "python" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "uvicorn" ]; then
  set -- "$@" --port "$PORT"
fi

if [ "$(id -u)" = "0" ]; then
  if [ "$PUID" = "0" ]; then
    echo "WARNUNG: PUID=0 – die Anwendung läuft als root. Empfohlen ist ein unprivilegierter Benutzer." >&2
  fi
  # Benutzer/Gruppe im Image an PUID/PGID anpassen (für Namensauflösung und Logs)
  groupmod -o -g "$PGID" app 2>/dev/null || true
  usermod -o -u "$PUID" -g "$PGID" app 2>/dev/null || true

  mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/logs" "$CONFIG_DIR/tmp"
  # Nur Einträge mit abweichendem Besitzer anpassen (schnell auch bei vielen Logdateien)
  find "$CONFIG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

  GROUPS_ARG="$PGID"
  if [ -n "${SUPPLEMENTARY_GROUPS:-}" ]; then
    GROUPS_ARG="$PGID,$(echo "$SUPPLEMENTARY_GROUPS" | tr ' ' ',')"
  fi
  echo "Starte Media Integrity Scanner als UID $PUID / GID $PGID (Gruppen: $GROUPS_ARG), Port $PORT"
  export HOME="$CONFIG_DIR"
  exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$GROUPS_ARG" --inh-caps=-all -- "$@"
fi

echo "Starte Media Integrity Scanner als UID $(id -u) / GID $(id -g)"
exec "$@"
