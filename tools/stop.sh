#!/usr/bin/env bash
# Stop the Control Limits gateway — pidfile first, then sweep ANY stray
# instances of this app.py (double-starts leave orphans that still hold the
# ports, and that is exactly what bit us once).
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDF="$BASE/data/gateway.pid"
KILLED=""

if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  kill "$(cat "$PIDF")" && KILLED="$KILLED $(cat "$PIDF")"
fi
rm -f "$PIDF"

# sweep: any pid whose cmdline contains this exact app.py path
for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  [ "$p" = "$$" ] && continue
  CMD="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
  case "$CMD" in
    *"$BASE/app.py"*|*"$BASE/run_central.py"*|*"$BASE/run_secondary.py"*)
      kill "$p" 2>/dev/null && KILLED="$KILLED $p"
      ;;
  esac
done

sleep 1
# stragglers -> SIGKILL
for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  CMD="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
  case "$CMD" in
    *"$BASE/app.py"*|*"$BASE/run_central.py"*|*"$BASE/run_secondary.py"*) kill -9 "$p" 2>/dev/null && KILLED="$KILLED $p" ;;
  esac
done

[ -n "$KILLED" ] && echo "Gateway stopped (pids:$KILLED)." || echo "Not running."
