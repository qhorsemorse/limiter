#!/usr/bin/env bash
# tools/lock_browsers.sh — disable EVERY browser on this machine except Firefox.
#
# Goal: the child (or anyone) cannot switch to Chromium/Chrome/Brave/Edge/
# Opera/... to escape the Control Limits gateway. Firefox is the ONLY browser
# that can work.
#
# Usage (run WITH sudo -- needs root to take the lock past the user accounts):
#   sudo bash tools/lock_browsers.sh             # lock everything not-Firefox
#   sudo bash tools/lock_browsers.sh --dry-run   # only print what it would do
#   sudo bash tools/lock_browsers.sh --unlock    # re-enable everything we locked
#   sudo bash tools/lock_browsers.sh --relock    # re-lock after an apt upgrade
#
# What it does (per found browser, Firefox excluded):
#   * snap packages  -> `snap disable <name>`   (reversible via --unlock)
#   * system binaries -> `chmod 000`            (reversible via --unlock;
#                            original mode is saved in data/browsers_locked.txt)
#   * .desktop launchers present alongside are chmod 000 too (menu item dies)
#
# Non-root users (the child account) CANNOT undo any of this: locked files are
# root-owned mode 000, and `snap enable` needs root.
#
# Caveats:
#   * An apt install/upgrade of a locked package can RESTORE its binary --
#     re-run with --relock afterwards (or install an apt post-invoke hook).
#   * Only browsers actually installed are affected; re-run after installs.
#   * Firefox (snap, classic, or any variant) is never touched.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIST="$BASE/data/browsers_locked.txt"
mkdir -p "$BASE/data"
[ -f "$LIST" ] || : > "$LIST"

MODE="lock"
case "${1:-}" in
  --dry-run) MODE="dry" ;;
  --unlock)  MODE="unlock" ;;
  --relock)  MODE="relock" ;;
  *) echo "usage: $0 [--dry-run|--unlock|--relock]"; exit 1 ;;
esac

EUID_NOW="$(id -u)"
if [ "$EUID_NOW" != "0" ] && [ "$MODE" != "dry" ]; then
  echo "This needs root to be irreversible for normal users. Re-run with:"
  echo "  sudo bash $BASE/tools/lock_browsers.sh $1"
  exit 1
fi

# Browser names / package stems we consider "a browser" (lowercase).
# Anything whose snap name or PATH binary matches these (or is obviously a
# browser binary) is a target. Firefox is explicitly excluded everywhere.
BROWSER_MATCH='chrom|brave|msedge|microsoft-edge|edge-|opera|vivaldi|epiphany|midori|konqueror|falkon|qutebrowser|waterfox|ungoogled|librewolf|floorp|palemoon|pale-moon|cuntnavigator|browser'

is_firefox() { case "$1" in *firefox*) return 0 ;; *) return 1 ;; esac; }

say()  { echo "  [${MODE}] $*"; }
run()  { if [ "$MODE" = "dry" ]; then say "$ (dry) $*"; else "$@"; fi; }

# record what we change so --unlock can restore it (idempotent); dry-run never writes
already_locked() { grep -q "^$1"$'\t' "$LIST" 2>/dev/null; }
add_list()  { if [ "$MODE" != "dry" ]; then printf '%s\n' "$*" >> "$LIST"; fi; }

echo "== Control Limits: browser lock ($MODE) =="

# ---------------- 1) snap packages ----------------
if [ -d /snap ]; then
  for d in /snap/*/; do
    name="$(basename "$d")"
    [ -d "$d/current" ] || continue          # only actually-installed snaps
    is_firefox "$name" && continue
    if echo "$name" | grep -qiE "$BROWSER_MATCH"; then
      if grep -q "^snap"$'\t'"$name"$'\t' "$LIST"; then
        say "snap $name: already in lock list (snap disable skipped)"
      else
        add_list "snap"$'\t'"$name"
        if [ "$MODE" = "unlock" ]; then
          run snap enable "$name"
        else
          run snap disable "$name"
        fi
      fi
    fi
  done
fi

# ---------------- 2) system binaries on PATH + /opt ----------------
TARGETS="$(mktemp)"
for d in /usr/bin /usr/local/bin /bin /opt; do
  [ -d "$d" ] || continue
  find "$d" -maxdepth 4 -type f -executable 2>/dev/null \
    | grep -iE "$BROWSER_MATCH" >> "$TARGETS" || true
done
# common wrapper names even if they don't match the regex
for w in gnome-www-browser x-www-browser sensible-browser; do
  p="$(command -v "$w" 2>/dev/null || true)"
  [ -n "$p" ] && [ ! -L "$p" ] && echo "$p" >> "$TARGETS"
done
sort -u "$TARGETS" -o "$TARGETS"

while IFS= read -r BIN; do
  [ -z "$BIN" ] && continue
  is_firefox "$BIN" && continue
  # skip false positives: dev libs, node modules, Electron runtime pieces of
  # NON-browser apps (LM Studio etc.), info tools, and the /usr/bin wrappers
  # (sensible-browser & co only exec the system default = firefox, so they
  # need no locking).
  case "$BIN" in
    */node_modules/*|*browserslist*|*libchromaprint*|*chrome-trace*|*electron-to-chromium*) continue ;;
    *infobrowser*|*crashpad*|*chrome-sandbox*|*nacl*|*swiftshader*) continue ;;
    *sensible-browser*|*gnome-www-browser*|*x-www-browser*) continue ;;
  esac
  # only touch real browser binaries, not shared libraries or python modules
  case "$BIN" in
    *.so*|*.py*) continue ;;
  esac
  BASENAME="$(basename "$BIN")"
  key="bin$(realpath -s "$BIN" 2>/dev/null || echo "$BIN")"
  if grep -q "^$key"$'\t' "$LIST"; then
    say "$BIN: already in lock list (skip)"
    continue
  fi
  mode="$(stat -c '%a' "$BIN" 2>/dev/null || echo 755)"
  add_list "$key"$'\t'"$BIN"$'\t'"$mode"
  if [ "$MODE" = "unlock" ]; then
    run chmod "$mode" "$BIN"
  else
    say "$BIN (mode $mode) -> chmod 000"
    run chmod 000 "$BIN"
  fi
done < "$TARGETS"
rm -f "$TARGETS"

# ---------------- 3) report ----------------
echo
if [ "$MODE" = "unlock" ]; then
  : > "$LIST"   # everything is back; clear the lock list
  echo "All locked browsers re-enabled. Lock list cleared."
else
  n=$(grep -c . "$LIST" || true)
  echo "Locked entries recorded in $LIST ($n). Firefox is untouched."
  [ "$MODE" = "dry" ] && echo "(dry run -- no changes made)"
  echo "Tip: after any 'apt upgrade' that touches a locked browser, re-run with --relock."
fi
