#!/bin/bash
# watch-429: rotate wgtunnel egress when claude-code-proxy hits upstream 429.
# Watches logs/last_upstream_failure.json (written by responses_client on
# HTTP>=400 AND on HTTP-200 error bodies like FreeUsageLimitError).
# Run from this dir:  nohup ./watch-429.sh >>/tmp/wgtunnel-429watch.log 2>&1 &
cd "$(dirname "$0")"
FILE="logs/last_upstream_failure.json"
PIDFILE="/tmp/wgtunnel-proxy.pid"
# Ignore failures that predate the watcher start.
last=""
[ -f "$FILE" ] && last=$(stat -c %Y "$FILE")
echo "$(date '+%F %T'): watch-429 started (ignoring $FILE @ $last)"
while true; do
  if [ -f "$FILE" ]; then
    cur=$(stat -c %Y "$FILE")
    st=$(python3 -c "import json;print(json.load(open('$FILE')).get('status',''))" 2>/dev/null)
    if [ "$cur" != "$last" ] && [ "$st" = "429" ]; then
      last="$cur"
      if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill -USR1 "$(cat "$PIDFILE")" && echo "$(date '+%F %T'): upstream 429, rotated wgtunnel"
      else
        echo "$(date '+%F %T'): upstream 429 but wgtunnel proxy not running"
      fi
    fi
  fi
  sleep 10
done
