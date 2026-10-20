#!/usr/bin/env bash
# Install the Control Limits SECONDARY (child device) gateway.
#
# The secondary runs on every non-central device. It starts at boot, runs as
# long as the device is in use, and enforces the limits that live on the
# CENTRAL server. The device's Firefox is pointed at this machine's DoH
# front-door (tools/setup_firefox.sh) — that is how the central's limits get
# enforced here.
#
# Usage:
#   sudo -n bash tools/install_secondary.sh <central-base-url>
# e.g.  bash tools/install_secondary.sh http://192.168.0.10:8080
#
# What it does (all user-space, no root needed):
#   1. writes  data/secondary.json  {"central": "<url>"}
#   2. ensures TLS certs exist (tools/gen_certs.sh) so the DoH front-door comes up
#   3. installs the systemd --user unit  limiter-secondary.service  (substituting
#      the base dir + central URL)
#   4. enables + starts it (auto at boot, Restart=always)
set -u

CENTRAL="${1:-${CONTROL_LIMITS_CENTRAL:-http://127.0.0.1:8080}}"

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="limiter-secondary.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
PY="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"

echo "[install] project base : $BASE"
echo "[install] central URL  : $CENTRAL"

# 1) config
mkdir -p "$BASE/data"
printf '{"central": "%s"}\n' "$CENTRAL" > "$BASE/data/secondary.json"
echo "[install] wrote $BASE/data/secondary.json"

# 2) certs (needed for the DoH front-door to come up)
if [ ! -f "$BASE/certs/server.pem" ] || [ ! -f "$BASE/certs/server.key" ]; then
  echo "[install] no certs found — running tools/gen_certs.sh"
  bash "$BASE/tools/gen_certs.sh"
fi

# 3) install the unit (substitute placeholders)
mkdir -p "$UNIT_DIR"
sed -e "s#__BASE__#$BASE#g" -e "s#__CENTRAL__#$CENTRAL#g" \
    "$BASE/tools/limiter-secondary.service" > "$UNIT_PATH"
echo "[install] unit installed at $UNIT_PATH"

# 4) enable + start (systemd --user, no root)
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
echo "           curl -sk https://127.0.0.1:8443/dns-query  (DoH front-door)"
