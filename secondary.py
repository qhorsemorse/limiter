#!/usr/bin/env python3
"""Control Limits — SECONDARY (child device) gateway.

Every non-central device runs this. Its *sole* purpose is to fetch all limit
information from the central server and enforce it on this device. It owns:

  * the DoH (TLS) front-door that the device's Firefox points at (:DOH_PORT,
    loopback) — this IS the enforcement channel, exactly like the central does
    for its own browser.
  * the device-local bypass / nuclear watcher (is THIS Firefox still routing
    through us? a VPN up? public DoH? lock tampered?) — nuclear is a local
    concern, independent of where the limits live.
  * a background sync ticker that keeps the local cache fresh (TTL-driven) and
    nudges the central's state machine via /api/detect when a domain is reached.
  * a child web UI: nuclear/offline status, a READ-ONLY view of the central's
    limits (mirrored in the local cache), query stats, and the per-device
    workout / reward timer.

Limits themselves are NOT stored locally in the source-of-truth sense. A local
cache DB (data/remote_cache.db) holds a COPY of the central limits table; it is
re-synced from the central as needed (see remote_store.RemoteStore). See
remote_store.py for the exact cache-first / staleness / offline (fail-closed)
semantics.

Lifecycle: meant to be started at boot (systemd --user) and to run as long as
the device is in use. See tools/install_secondary.sh.
"""
import argparse, json, os, re, threading, time, signal, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import dns as DNS, engine as E, bypass as B, doh as DOH, workout as W, work as WK
from remote_store import RemoteStore

DATA = os.path.join(BASE, "data")
DEFAULT_CENTRAL = os.environ.get("CONTROL_LIMITS_CENTRAL", "http://127.0.0.1:8080")
CACHE_DB = os.environ.get("REMOTE_CACHE_DB", os.path.join(DATA, "remote_cache.db"))
WORKOUT_FILE = os.environ.get("WORKOUT_FILE", os.path.join(DATA, "workout.json"))
INDEX_HTML = open(os.path.join(BASE, "secondary_index.html"), "r", encoding="utf-8").read()
WORKOUT_HTML = open(os.path.join(BASE, "workout.html"), "r", encoding="utf-8").read()
WORK_HTML = open(os.path.join(BASE, "work.html"), "r", encoding="utf-8").read()

_state = {"last": {}, "streak": 0}


def _load_central():
    """Config source of truth: env CONTROL_LIMITS_CENTRAL, else data/secondary.json,
    else the built-in default (point this at your central server)."""
    try:
        with open(os.path.join(DATA, "secondary.json")) as f:
            return json.load(f).get("central", DEFAULT_CENTRAL).rstrip("/")
    except Exception:
        return DEFAULT_CENTRAL


# ---------------- HTTP handler (child view) ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ControlLimits-Secondary/1.0"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code, None if code == 204 else "OK")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and code != 204:
            self.wfile.write(body)
            self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path == "/workout.html":
            return self._send(200, WORKOUT_HTML, "text/html; charset=utf-8")
        if path == "/work.html":
            return self._send(200, WORK_HTML, "text/html; charset=utf-8")
        if path == "/api/limits":
            # READ-ONLY mirror of the central's limits, pulled from the local cache.
            return self._send(200, [self._view(l) for l in STORE.snapshot()])
        if path == "/api/remote/status":
            return self._send(200, self._status())
        if path == "/api/bypass":
            return self._send(200, get_view_bypass())
        if path == "/api/stats":
            s = STORE
            return self._send(200, {"udp": s.stats["udp"], "doh": s.stats["doh"],
                                    "online": bool(s.online), "emergency": bool(s.emergency)})
        if path == "/api/workout":
            return self._send(200, WORKOUT.status())
        if path == "/api/work":
            return self._send(200, WORK.status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/workout":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                rec = json.loads(self.rfile.read(length).decode() if length else "{}")
                action = rec.get("action")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            w = WORKOUT
            if action == "start":
                return self._send(200, w.start())
            if action == "tick":
                st, err = w.tick(rec.get("delta_ms", 0))
                return self._send(400 if err else 200, {**st, **(None if not err else {"error": err})})
            if action == "redeem":
                st, err = w.redeem(STORE)
                return self._send(403 if err else 200, {**st, **(None if not err else {"error": err})})
            if action == "set_duration":
                st, err = w.set_duration(rec.get("ms"))
                return self._send(400 if err else 200, {**st, **(None if not err else {"error": err})})
            return self._send(400, {"error": "unknown action"})
        if path == "/api/work":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                rec = json.loads(self.rfile.read(length).decode() if length else "{}")
                action = rec.get("action")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            w = WORK
            st = w.status()
            if action == "add_card":
                st = w.add_card(rec.get("name"), rec.get("kind"))
            elif action == "remove_card":
                st = w.remove_card(rec.get("cid"))
            elif action == "set_budget":
                st = w.set_budget(rec.get("ms"))
            elif action == "set_limit":
                st = w.set_limit(rec.get("ms"))
            elif action == "set_penalty":
                st = w.set_penalty(rec.get("ms"))
            elif action == "set_active_card":
                st = w.set_active_card(rec.get("cid"))
            elif action == "start":
                st = w.start()
            elif action == "pause":
                st = w.pause()
            elif action == "resume":
                st = w.resume()
            elif action == "tick":
                st, err = w.tick(rec.get("delta_ms", 0))
                if err:
                    return self._send(400 if err else 200, {**st, **(None if not err else {"error": err})})
            else:
                return self._send(400, {"error": "unknown action"})
            # keep the store's block-eom-thing window in sync with the session
            STORE.set_work_until(st.get("work_until_ms", 0))
            return self._send(200, st)
        return self._send(404, {"error": "not found"})

    # ---------------- helpers ----------------
    def _view(self, l):
        """Trim the cached limit to what the child view needs."""
        return {
            "id": l.get("id"), "name": l.get("name"), "type": l.get("type"),
            "domains": l.get("domains", []), "grace_ms": l.get("grace_ms"),
            "cooldown_ms": l.get("cooldown_ms"), "state": l.get("state"),
            "grace_until": l.get("grace_until"), "cooldown_until": l.get("cooldown_until"),
            "updated_at": l.get("updated_at") or l.get("created_at"),
        }

    def _status(self):
        return {
            "online": bool(STORE.online),
            "emergency": bool(STORE.emergency),
            "last_sync_ms": int(STORE.last_sync_ms),
            "central": CENTRAL,
            "nuclear": bool(STORE.nuclear),
            "effective_nuclear": STORE.is_nuclear(),
            "override": bool(STORE.override),
            "num_limits": len(STORE.snapshot()),
        }


def get_view_bypass():
    s = STORE
    now = E.now_ms()
    return {
        "detected": _state["last"],
        "nuclear": s.nuclear,
        "effective_nuclear": s.is_nuclear() and not s.reward_active(now),
        "reason": s.nuclear_reason,
        "override": s.override,
        "reward": {"active": s.reward_active(now),
                   "left_ms": int(max(0, (s.reward_until or 0) - now))},
    }


# ---------------- background ticker: keep sync fresh + report accesses ----------------
def sync_ticker(store, interval=5.0):
    """Periodically refresh the local cache (TTL-driven inside tick) so limit
    changes made on the central are picked up, and so we notice when the central
    comes back online after being unreachable."""
    while True:
        try:
            store.tick(E.now_ms())
        except Exception:
            pass
        time.sleep(interval)


# ---------------- bypass watcher (device-local) ----------------
def bypass_watcher(store, doh_port, interval=5.0,
                    lan_gw=None, uncontrolled_streak=3):
    """Device-local bypass detection. Same logic as app.py's watcher but using
    the RemoteStore's nuclear/override state. Nuclear is independent of where the
    limits come from."""
    was_nuclear = False
    streak = 0
    uncontrolled_streak = uncontrolled_streak or 3
    while True:
        try:
            d = B.detect(lan_gateway=lan_gw, doh_port=doh_port)
            nuclear, reason = False, None
            if d["vpn"]["active"]:
                nuclear, reason = True, ("VPN detected: %s" % d["vpn"]["why"])
            elif d["public_doh"]["active"]:
                nuclear, reason = True, ("Public DoH in use (bypass!): %s" % ", ".join(d["public_doh"]["peers"]))
            off = d.get("off_dns") or {"active": False, "peers": []}
            if off["active"]:
                streak += 1
                if streak >= 2:
                    nuclear, reason = True, ("Firefox resolving OUTSIDE the gateway: %s" % ", ".join(off["peers"]))
            else:
                streak = 0
            _state["last"] = d
            store.set_nuclear(nuclear, reason if (nuclear and not store.override) else None)
            eff = store.is_nuclear()
            if eff != was_nuclear:
                print(("[!] BYPASS -> nuclear ON: %s" % reason) if eff
                      else "[+] nuclear OFF (bypass cleared)")
                was_nuclear = eff
        except Exception as e:
            print("[!] watcher error:", e)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--central", default=None, help="central base URL (overrides config/env)")
    ap.add_argument("--web", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--dohport", type=int, default=int(os.environ.get("DOH_PORT", "8443")))
    ap.add_argument("--certdir", default=os.environ.get("CERTDIR", os.path.join(BASE, "certs")))
    ap.add_argument("--uphost", default=os.environ.get("DNS_UP", "127.0.0.53"))
    ap.add_argument("--upport", type=int, default=53)
    ap.add_argument("--lan-gw", default=os.environ.get("LAN_GW", ""))
    ap.add_argument("--ttl", type=float, default=float(os.environ.get("REMOTE_TTL", "30")),
                    help="local-cache TTL seconds before re-syncing from central")
    ap.add_argument("--bypass-interval", type=float, default=5.0)
    ap.add_argument("--allow-free-offline", dest="allow_free_offline", action="store_true",
                    help="while offline, forward unrestricted sites (fail-open; default is fail-closed)")
    args = ap.parse_args()
    global CENTRAL, STORE, HTML, WORKOUT, WORK

    CENTRAL = args.central or _load_central()

    os.makedirs(DATA, exist_ok=True)
    # prime the cache (syncs once at startup)
    STORE = RemoteStore(CACHE_DB, CENTRAL, ttl_seconds=args.ttl,
                        allow_free_offline=args.allow_free_offline)
    HTML = INDEX_HTML.replace("__CENTRAL__", CENTRAL)

    WORKOUT = W.Workout(WORKOUT_FILE)
    WORK = WK.Work(os.environ.get("WORK_FILE", os.path.join(DATA, "work.json")))
    # restore any in-progress reward / work-focus window across restarts (in-memory
    # flag only — without this a restart mid-session would un-block everything).
    try:
        wst = WORKOUT.status()
        if wst.get("reward_until_ms"):
            STORE.set_reward_until(wst["reward_until_ms"])
    except Exception:
        pass
    try:
        _wku = WORK.status().get("work_until_ms", 0) or 0
        if _wku:
            STORE.set_work_until(_wku)
    except Exception:
        pass

    upstream = (args.uphost, args.upport)

    # DoH front-door (Firefox's entry on this device) — resolves via STORE.
    doh_httpd = None
    if DOH.tls_available(args.certdir):
        doh_httpd = DOH.serve(STORE, port=args.dohport, upstream=upstream, certdir=args.certdir)
        print("[+] secondary DoH front-door on https://127.0.0.1:%d/dns-query" % args.dohport)
    else:
        print("[!] No TLS certs in %s — DoH front-door DISABLED (run tools/gen_certs.sh on this device)" % args.certdir)

    threading.Thread(target=sync_ticker, args=(STORE,), daemon=True, name="sync-ticker").start()
    threading.Thread(target=bypass_watcher,
                     args=(STORE, args.dohport, args.bypass_interval, args.lan_gw),
                     daemon=True, name="bypass-watcher").start()

    httpd = ThreadingHTTPServer(("0.0.0.0", args.web), Handler)
    print("[+] secondary 'upstream' (real DNS for allowed sites): %s:%d" % upstream)
    print("[+] secondary child UI + API on http://0.0.0.0:%d" % args.web)
    print("[+] central: %s (initial sync online=%s emergency=%s)"
          % (CENTRAL, STORE.online, STORE.emergency))

    def shutdown(signum, frame=None):
        print("[+] secondary shutting down")
        doh_httpd.server.shutdown() if doh_httpd else None
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    httpd.serve_forever()


if __name__ == "__main__":
    main()
