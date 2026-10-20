#!/usr/bin/env bash
# Install the DoH provider LOCK as a Firefox enterprise policy.
#
# This Firefox version (Linux) reads policies ONLY from:
#   /etc/firefox/policies/policies.json          (system-wide, needs root)  <- we use this
#   <install>/distribution/policies.json         (snap squashfs: read-only, impossible)
#   $XDG_RUNTIME_DIR/policies.json               (only with the perUserDir pref;
#                                                 not honored in the snap confinement)
# The profile-area defaults/policies.json location used by older Firefox
# versions is IGNORED by this build (verified via about:policies: inactive).
#
# Effect: the DoH provider is pinned to the local gateway. The Settings UI
# lock makes "DNS Over HTTPS" read-only -- users cannot turn DoH off or
# switch to Cloudflare/Google/custom. Effective on next Firefox start.
set -u
POL_TARGET="/etc/firefox/policies/policies.json"
TMP="$(mktemp)"
cat > "$TMP" <<'JSON'
{
  "policies": {
    "DNSOverHTTPS": {
      "Enabled": true,
      "Fallback": false,
      "Locked": true,
      "ProviderURL": "https://127.0.0.1:8443/dns-query"
    }
  }
}
JSON
if sudo -n true 2>/dev/null; then
  # root available without password
  sudo -n mkdir -p /etc/firefox/policies
  sudo -n install -m 644 -o root -g root "$TMP" "$POL_TARGET"
else
  # prompts for the password
  sudo mkdir -p /etc/firefox/policies
  sudo install -m 644 -o root -g root "$TMP" "$POL_TARGET"
fi
rm -f "$TMP"
echo "installed: $POL_TARGET"
cat "$POL_TARGET"
echo
echo "Now QUIT Firefox fully and open it again."
echo "Check: about:policies should list DNS Over HTTPS, and"
echo "Settings -> General -> DNS Over HTTPS must be read-only (locked)."
