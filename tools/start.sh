#!/usr/bin/env bash
# Start the Control Limits gateway (plain-DNS sinkhole :5354, DoH :8443, web UI :8080).
# Refuses to double-start if ANY process already serves :8080 (orphan-proof).
set -u
export PYTHONUNBUFFERED=1    # keep data/gateway.log live instead of block-buffered
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDF="$BASE/data/gateway.pid"
LOG="$BASE/data/gateway.log"
mkdir -p "$BASE/data"

port_busy() {  # tcp connect test
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}
if port_busy 8080; then
  echo "Something already serves :8080 — the gateway (or an orphan) is running."
  echo "Run tools/stop.sh first, then retry."
  exit 1
fi

nohup python3 "$BASE/app.py" --port 5354 --web 8080 --dohport 8443 --uphost 127.0.0.53 \
  >> "$LOG" 2>&1 &
PID=$!
disown $PID 2>/dev/null || true
echo $PID > "$PIDF"

sleep 2.5
if kill -0 "$PID" 2>/dev/null && port_busy 8080 && port_busy 8443; then
  echo "Gateway started (pid $PID)."
  echo "  Web UI : http://127.0.0.1:8080"
  echo "  DoH    : https://127.0.0.1:8443/dns-query"
  echo "  Log    : data/gateway.log"
  tail -4 "$LOG"
else
  echo "Gateway failed to start -- last log lines:"
  tail -8 "$LOG"
  kill "$PID" 2>/dev/null
  rm -f "$PIDF"
  exit 1
fi
