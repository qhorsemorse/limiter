#!/usr/bin/env bash
# Install the Control Limits CENTRAL server.
#
# The central holds all limits (the single source of truth) and serves the
# admin web UI + JSON API only. Nobody browses through it, so the plain-DNS
# sinkhole, the DoH front-door, and the device-local bypass watcher are OFF.
#
# Usage:
#   sudo -n bash tools/install_central.sh
#
# What it does (user-space, no root needed):
#   1. ensures data/ exists
#   2. installs the systemd --user unit limiter-central.service
#      (substituting the base dir) and enables + starts it (auto at boot)
set -u

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="limiter-central.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

echo "[install] project base : $BASE"

# 1) state dir
mkdir -p "$BASE/data"
echo "[install] ensured $BASE/data"

# 2) install the unit (substitute placeholders)
mkdir -p "$UNIT_DIR"
sed -e "s#__BASE__#$BASE#g" \
    "$BASE/tools/limiter-central.service" > "$UNIT_PATH"
echo "[install] unit installed at $UNIT_PATH"

# 3) enable + start (systemd --user, no root)
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now "$UNIT_NAME" 2>/dev/null \
    && echo "[install] enabled + started (auto at boot)" \
    || { echo "[install] systemctl failed — enabling manually";
         systemctl --user enable "$UNIT_NAME" 2>/dev/null;
         systemctl --user start "$UNIT_NAME" 2>/dev/null; }
  echo "[install] status:"
  systemctl --user is-active "$UNIT_NAME" 2>/dev/null
else
  echo "[install] systemctl not available — run manually:"
  echo "           systemctl --user daemon-reload"
  echo "           systemctl --user enable --now $UNIT_NAME"
fi

echo "[install] done. Verify:"
echo "           systemctl --user status $UNIT_NAME"
echo "           journalctl --user -u $UNIT_NAME -f"
echo "           open http://127.0.0.1:8080  (admin UI — add limits here)"
