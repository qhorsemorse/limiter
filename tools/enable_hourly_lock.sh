#!/usr/bin/env bash
# ONE-TIME setup: let the Control Limits gateway (running as a normal user)
# re-lock non-Firefox browsers hourly, without a password, by giving that
# user passwordless sudo for tools/lock_browsers.sh ONLY.
#
# Creates /etc/sudoers.d/limiter-lock, validates it with visudo, and shows
# the exact rule. Re-running is idempotent; to revoke later:
#   sudo rm /etc/sudoers.d/limiter-lock
set -eu
TARGET="/home/pietro/limiter/tools/lock_browsers.sh"
[ -x "$TARGET" ] || { echo "lock script missing: $TARGET"; exit 1; }
RULE="$(id -un) ALL=(root) NOPASSWD: $TARGET"
FILE="/etc/sudoers.d/limiter-lock"

if [ "$(id -u)" = "0" ]; then
  printf '%s\n' "$RULE" > "$FILE"
  chmod 440 "$FILE"
  visudo -cf "$FILE"
else
  sudo tee "$FILE" >/dev/null <<EOF
$RULE
EOF
  sudo chmod 440 "$FILE"
  sudo visudo -cf "$FILE"
fi
echo
echo "Installed rule:"
cat "$FILE"
echo
echo "Done. The gateway will now re-run the browser lock hourly on its own."
