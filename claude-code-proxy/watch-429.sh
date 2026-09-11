#!/bin/bash
# watch-429: rotate wgtunnel egress when claude-code-proxy hits upstream 429.
# Watches logs/last_upstream_failure.json (written by responses_client on
# HTTP>=400 AND on HTTP-200 error bodies like FreeUsageLimitError).
# Run from this dir:  nohup ./watch-429.sh >>/tmp/wgtunnel-429watch.log 2>&1 &
cd "$(dirname "$0")"
FILE="logs/last_upstream_failure.json"
ARCHIVE_DIR="logs/failures"
PIDFILE="/tmp/wgtunnel-proxy.pid"
# Debounce: upstream rewrites the file on EVERY failed attempt, so without
# this one 429 incident would rotate every 10s and get us server-side
# throttled. One rotation per incident, then silence for 5 min.
COOLDOWN_SECS=300
next_allowed=0
# Ignore failures that predate the watcher start.
last=""
[ -f "$FILE" ] && last=$(stat -c %Y "$FILE")
echo "$(date '+%F %T'): watch-429 started (ignoring $FILE @ $last)"
# Archive a handled failure: the rotation (or the skip) consumed this
# signal, so move it out of the live slot into logs/failures/ (keeps the
# dashboard green instead of re-alerting on an already-handled 429).
archive_handled() {
  mkdir -p "$ARCHIVE_DIR"
  ts=$(date '+%Y%m%d-%H%M%S')
  # keep only the newest 20 archives
  mv "$FILE" "$ARCHIVE_DIR/failure-${ts}.json" 2>/dev/null
  ls -t "$ARCHIVE_DIR"/failure-*.json 2>/dev/null | tail -n +21 | xargs -r rm -f
  last=""
}

while true; do
  if [ -f "$FILE" ]; then
    cur=$(stat -c %Y "$FILE")
    st=$(python3 -c "import json;print(json.load(open('$FILE')).get('status',''))" 2>/dev/null)
    if [ "$cur" != "$last" ] && [ "$st" = "429" ]; then
      last="$cur"
      now=$(date +%s)
      if [ "$now" -ge "$next_allowed" ]; then
        next_allowed=$((now + COOLDOWN_SECS))
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
          kill -USR1 "$(cat "$PIDFILE")" && echo "$(date '+%F %T'): upstream 429, rotated wgtunnel (cooldown ${COOLDOWN_SECS}s)"
          archive_handled
        else
          echo "$(date '+%F %T'): upstream 429 but wgtunnel proxy not running"
        fi
      else
        echo "$(date '+%F %T'): upstream 429 within cooldown, skipping"
        archive_handled
      fi
    fi
  fi
  sleep 10
done
