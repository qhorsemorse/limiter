#!/usr/bin/env bash
# Stop and disable the Control Limits SECONDARY gateway on this device.
# Usage: bash tools/stop_secondary.sh   (also removes the unit)
#        bash tools/stop_secondary.sh --keep-unit   (stop only, keep the unit)
set -u

KEEP_UNIT="${1:-false}"
UNIT_NAME="limiter-secondary.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

echo "[stop] disabling + stopping $UNIT_NAME"
systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
systemctl --user disable "$UNIT_NAME" 2>/dev/null || true

if [ "$KEEP_UNIT" != "--keep-unit" ] && [ -f "$UNIT_PATH" ]; then
  rm -f "$UNIT_PATH"
  echo "[stop] removed unit $UNIT_PATH"
fi

systemctl --user daemon-reload 2>/dev/null || true
echo "[stop] done."
