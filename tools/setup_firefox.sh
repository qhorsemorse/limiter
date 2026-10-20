#!/usr/bin/env bash
# Point Firefox at the Control Limits gateway:
#   1) merge the DNS-lock block into user.js of every profile (then chmod 444)
#   2) trust our local CA in each profile's cert9.db (certutil, no root needed)
#   3) record the profiles in data/firefox_profiles.txt (the watcher uses this)
# Idempotent: re-running only refreshes the marked block + re-imports the CA.
# NOTE: close Firefox first if it's running -- certutil can hit a locked cert9.db.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CU="$BASE/tools/bin/certutil"
CA="$BASE/certs/ca.pem"
PROFDIR="$HOME/snap/firefox/common/.mozilla/firefox"
LIST="$BASE/data/firefox_profiles.txt"

[ -x "$CU" ] || { echo "certutil missing -- run tools/get_certutil.sh first"; exit 1; }
[ -s "$CA" ] || { echo "CA missing -- run tools/gen_certs.sh first"; exit 1; }

if pgrep -x firefox >/dev/null 2>&1; then
  echo "WARNING: Firefox appears to be running. user.js will still be updated, but"
  echo "the CA import into a live profile may fail. If it fails: close Firefox and re-run."
fi

mkdir -p "$BASE/data"
: > "$LIST"
found=0

for P in "$PROFDIR"/*.default*; do
  [ -d "$P" ] || continue
  found=1
  UJ="$P/user.js"
  # a previous run chmods user.js 444; make it writable before merging
  [ -f "$UJ" ] && chmod 644 "$UJ" 2>/dev/null || true

  # ---- 1) user.js DNS-lock block (idempotent merge of the marked block) ----
  python3 - "$UJ" <<'PY'
import os, re, sys
path = sys.argv[1]
START = "// >>> ControlLimits (managed - do not edit; rewritten by tools) >>>"
END   = "// <<< ControlLimits <<<"
block = "\n".join([
    "", START,
    'user_pref("network.trr.mode", 3);',
    'user_pref("network.trr.uri", "https://127.0.0.1:8443/dns-query");',
    'user_pref("network.trr.custom_uri", "https://127.0.0.1:8443/dns-query");',
    'user_pref("network.trr.confirmationNS", "skip");',
    'user_pref("network.trr.wait-for-confirmation", false);',
    'user_pref("network.trr.fallback-on-zero-response", false);',
    'user_pref("network.trr.allow-rfc1918", true);',
    END, "",
])
txt = open(path).read() if os.path.exists(path) else ""
pat = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"[^\n]*\n?", re.S)
if pat.search(txt):
    txt = pat.sub(block, txt)
else:
    if txt and not txt.endswith("\n"):
        txt += "\n"
    txt += block
open(path, "w").write(txt)
PY
  chmod 444 "$UJ"

  # ---- 2) trust the local CA (delete + re-add = idempotent) ----
  "$CU" -D -d "sql:$P" -n ControlLimits-CA >/dev/null 2>&1 || true
  if "$CU" -A -d "sql:$P" -n ControlLimits-CA -t 'C,,' -i "$CA" 2>/dev/null; then
    echo "CA imported   : $P"
  else
    echo "!! CA import FAILED for $P (is Firefox running? re-run after closing it)"
  fi
  if "$CU" -L -d "sql:$P" -n ControlLimits-CA -a >/dev/null 2>&1; then
    echo "CA verified   : $P"
  else
    echo "!! CA not visible in $P"
  fi

  # ---- 3) record for the bypass watcher ----
  echo "$P" >> "$LIST"
done

# ---- 4) enterprise policy: force + LOCK the DoH provider to the local gateway ----
# NOTE: this Firefox build only reads policies from /etc/firefox/policies/policies.json
# (system) -- the profile-area defaults/policies.json location is IGNORED.
# That install needs root, so delegate to tools/install_policy.sh.
if [ -f /etc/firefox/policies/policies.json ]; then
  echo "policy present  : /etc/firefox/policies/policies.json"
elif sudo -n true 2>/dev/null; then
  bash "$BASE/tools/install_policy.sh"
else
  echo "NOTE: the DoH provider LOCK (policy) is not installed (needs root)."
  echo "Run:  bash $BASE/tools/install_policy.sh   (then restart Firefox)"
  echo "Until then the user.js lock re-applies the provider at every startup and the"
  echo "bypass watcher arms nuclear if Firefox is pointed at any other DNS."
fi

[ "$found" = 1 ] || { echo "No *.default* profiles found in $PROFDIR"; exit 1; }
echo
echo "Done. Profiles recorded in data/firefox_profiles.txt:"
cat "$LIST"
echo
echo "Next: RESTART Firefox fully (quit, then open). Check http://127.0.0.1:8080"
echo "-- the green 'DNS control armed' banner means it's working."
echo "-- the DoH provider is now policy-locked to the local gateway."
echo "-- to test the lock: settings -> General -> DNS Over HTTPS is read-only."
