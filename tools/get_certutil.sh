#!/usr/bin/env bash
# Grab certutil without root/sudo: download the libnss3-tools .deb, extract it
# with dpkg -x (no install, no sudo). Idempotent.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$BASE/tools/bin/certutil"
if [ -s "$BIN" ]; then
  echo "certutil already extracted at $BIN -- skipping."
  exit 0
fi
ARCH="$(dpkg --print-architecture)"
mkdir -p "$BASE/tools/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Order matters: 3.108 is proven to run against the system NSS on this box.
for VER in 3.108-1ubuntu1 3.98-1build1 3.120-1ubuntu2; do
  URL="http://archive.ubuntu.com/ubuntu/pool/main/n/nss/libnss3-tools_${VER}_${ARCH}.deb"
  echo "Trying $URL"
  if curl -fsSL -o "$TMP/pkg.deb" "$URL"; then
    dpkg -x "$TMP/pkg.deb" "$TMP/extract"
    if [ -x "$TMP/extract/usr/bin/certutil" ]; then
      cp "$TMP/extract/usr/bin/certutil" "$BIN"
      chmod +x "$BIN"
      echo "Installed certutil from libnss3-tools ${VER} (${ARCH})."
      exit 0
    fi
  fi
done
echo "All downloads failed. Check the network, or manually: curl -O $URL && dpkg -x pkg.deb $BASE/tools && cp $BASE/tools/usr/bin/certutil $BIN"
exit 1
