#!/usr/bin/env bash
# One-shot repair: remove the manually-entered (broken) DoH custom provider
# from Firefox prefs.js so the managed DNS-lock in user.js takes over on the
# next Firefox start.
#
# WHY: typing a bare "127.0.0.1" into the DoH provider field stores
#       network.trr.custom_uri = "http://127.0.0.1" (plain HTTP, wrong port),
#       which breaks TRR with TRR_DECODE_FAILED and keeps DNS out of the gateway.
#
# RULE: Firefox must be CLOSED, otherwise it rewrites prefs.js on exit and
#       overwrites this repair.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIST="$BASE/data/firefox_profiles.txt"
PROFDIR="$HOME/snap/firefox/common/.mozilla/firefox"

if [ ! -f "$LIST" ] || [ ! -s "$LIST" ]; then
  mkdir -p "$BASE/data"
  for P in "$PROFDIR"/*.default*; do
    [ -d "$P" ] && echo "$P" >> "$LIST"
  done
fi

[ -f "$LIST" ] || { echo "no profile list at $LIST (run tools/setup_firefox.sh)"; exit 1; }
if pgrep -x firefox >/dev/null 2>&1; then
  echo "Firefox is still RUNNING. Close every Firefox window, wait a moment, then re-run."
  exit 1
fi
found=0
while IFS= read -r P; do
  [ -d "$P" ] || continue
  PF="$P/prefs.js"
  [ -f "$PF" ] || continue
  found=1
  cp "$PF" "$PF.bak-$(date +%s)"
  python3 - "$PF" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
# drop any custom provider, mode, uri, exclusions, confirmations
text = re.sub(r'user_pref\("network\.trr\.(custom_uri|custom_set_using_default_uri|excluded-domains|mode|uri|confirmationNS|wait-for-confirmation|fallback-on-zero-response|allow-rfc1918)",[^;]*;\n?', '', text)
if not text.endswith("\n"):
    text += "\n"
text += ('user_pref("network.trr.mode", 3);\n'
         'user_pref("network.trr.uri", "https://127.0.0.1:8443/dns-query");\n'
         'user_pref("network.trr.custom_uri", "https://127.0.0.1:8443/dns-query");\n'
         'user_pref("network.trr.confirmationNS", "skip");\n'
         'user_pref("network.trr.wait-for-confirmation", false);\n'
         'user_pref("network.trr.fallback-on-zero-response", false);\n'
         'user_pref("network.trr.allow-rfc1918", true);\n')
open(path, "w").write(text)
print("cleaned:", path)
PY
done < "$LIST"
[ "$found" = 1 ] || { echo "no prefs.js found in listed profiles"; exit 1; }
echo "Done. Open Firefox normally -- it will resolve through the gateway automatically."
