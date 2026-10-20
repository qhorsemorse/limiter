#!/usr/bin/env python3
"""Control Limits - main app.

Serves:
  GET  /                   -> web frontend
  GET  /api/limits         -> JSON list of limits (+ live state)
  POST /api/limits         -> add a limit  (body: {"name","type","domains",grace?,cooldown?})
  DELETE /api/limits/<id>  -> delete a limit (admin)
  POST /api/detect         -> secondary reports a domain was reached (arms the
      central grace timer once); returns affected limits (authoritative state)
  GET  /api/bypass         -> live bypass detection (VPN / public DoH / browser state)
  POST /api/bypass/override -> {"enabled":bool} parent override of nuclear mode
  GET  /api/stats          -> query counters per channel (plain-DNS / DoH)
  GET  /api/health         -> {"ok":true}

Also runs:
  * the plain-DNS sinkhole (UDP+TCP; high port, no root) — legacy/aux path
  * the DoH (TLS) responder — the browser front-door (Firefox pointed at it)
  * a background ticker for time-limits
  * a bypass watcher that arms nuclear mode (block ALL) when a VPN, a public
    DoH endpoint, a tampered profile or an out-of-control browser is detected
"""
import argparse, json, os, re, subprocess, threading, time, signal, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
import db as DB, store as ST, dns as DNS, engine as E, bypass as B, doh as DOH, workout as W

_workout = None
_workout_lock = threading.Lock()

def get_workout():
    global _workout
    if _workout is None:
        with _workout_lock:
            if _workout is None:
                _workout = W.Workout(
                    os.environ.get("WORKOUT_FILE")
                    or os.path.join(BASE, "data", "workout.json"))
    return _workout

HTML = open(os.path.join(BASE, "index.html"), "r", encoding="utf-8").read()
DB_PATH = os.environ.get("LIMITS_DB", os.path.join(BASE, "data", "limits.db"))
PROFILES_FILE = os.environ.get("PROFILES_FILE", os.path.join(BASE, "data", "firefox_profiles.txt"))
LOCK_MARKER = "// >>> ControlLimits"

_store = None

def get_store(db_path=None):
    global _store
    if _store is None:
        p = db_path or DB_PATH
        _store = ST.Store(p)
    return _store

# ---------------- profile lock check ----------------
def profile_lock_ok():
    """True unless tools/setup_firefox.sh recorded profiles whose user.js
    DNS-lock block has been removed/edited."""
    prof_file = os.environ.get("PROFILES_FILE", PROFILES_FILE)
    if not os.path.exists(prof_file):
        return True, "no controlled profiles"
    try:
        profiles = [l.strip() for l in open(prof_file) if l.strip()]
    except OSError:
        return True, "unreadable profile list"
    bad = []
    for prof in profiles:
        uj = os.path.join(prof, "user.js")
        try:
            txt = open(uj, encoding="utf-8", errors="ignore").read()
        except OSError:
            bad.append(os.path.basename(prof) + ": user.js missing")
            continue
        if LOCK_MARKER not in txt or "// <<< ControlLimits" not in txt:
            bad.append(os.path.basename(prof) + ": user.js modified")
    return (not bad), ("; ".join(bad) if bad else "locked")

# ---------------- HTTP handler ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ControlLimits/1.1"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code, None if code == 204 else "OK")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if body and code != 204:
            self.wfile.write(body)
            self.wfile.flush()

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/workout.html":
            try:
                with open(os.path.join(BASE, "workout.html"), "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "limits": len(get_store().snapshot())})
            return
        if path == "/api/limits":
            limits = []
            for l in get_store().snapshot():
                limits.append({
                    "id": l["id"], "name": l["name"], "type": l["type"],
                    "domains": l["domains"], "grace_ms": l.get("grace_ms"),
                    "cooldown_ms": l.get("cooldown_ms"), "state": l["state"],
                    "grace_until": l.get("grace_until"),
                    "cooldown_until": l.get("cooldown_until"),
                    # remote secondaries key their cache-TTL (staleness) off this
                    "updated_at": l.get("updated_at") or l.get("created_at"),
                })
            self._send(200, limits)
            return
        if path == "/api/bypass":
            self._send(200, get_view_bypass())
            return
        if path == "/api/stats":
            s = get_store()
            self._send(200, {"udp": s.stats["udp"], "doh": s.stats["doh"]})
            return
        if path == "/api/workout":
            self._send(200, get_workout().status())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/detect":
            """Secondary devices POST an actual access to `domain` so the
            central arms the grace timer (once, centrally) on any matching free
            time-limit. Returns the affected limits (authoritative state) so the
            secondary folds them into its local cache copy."""
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                rec = json.loads(raw.decode() or "{}")
            except Exception as e:
                return self._send(400, {"error": "invalid json: " + str(e)})
            affected = get_store().report_access(rec.get("domain"))
            return self._send(200, {"detected": affected})
        if path == "/api/limits":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                rec = json.loads(raw.decode() or "{}")
            except Exception as e:
                return self._send(400, {"error": "invalid json: " + str(e)})
            # validate
            t = rec.get("type")
            if t not in ("absolute", "time"):
                return self._send(400, {"error": "type must be absolute or time"})
            domains = E.valid_domains(rec.get("domains", []))
            if not domains:
                return self._send(400, {"error": "at least one valid domain required"})
            if t == "time" and not (rec.get("grace_min") and rec.get("cooldown_min")):
                return self._send(400, {"error": "time limits need grace_min and cooldown_min"})
            try:
                new = get_store().add(rec)
            except Exception as e:
                return self._send(500, {"error": "db error: " + str(e)})
            self._send(201, {k: new[k] for k in ("id", "name", "type", "domains",
                        "grace_ms", "cooldown_ms", "state")})
            return
        if path == "/api/bypass/override":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                rec = json.loads(raw.decode() or "{}")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            get_store().set_override(bool(rec.get("enabled")))
            return self._send(200, {"override": bool(rec.get("enabled"))})
        if path == "/api/workout":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                rec = json.loads(raw.decode() or "{}")
                action = rec.get("action")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            w = get_workout()
            if action == "start":
                return self._send(200, w.start())
            if action == "tick":
                st, err = w.tick(rec.get("delta_ms", 0))
                if err:
                    return self._send(400, {"error": err, **st})
                return self._send(200, st)
            if action == "redeem":
                st, err = w.redeem(get_store())
                if err:
                    return self._send(403, {"error": err, **st})
                return self._send(200, st)
            if action == "set_duration":
                st, err = w.set_duration(rec.get("ms"))
                if err:
                    return self._send(400, {"error": err, **st})
                return self._send(200, st)
            return self._send(400, {"error": "unknown action (use start|tick|redeem|set_duration)"})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.match(r"^/api/limits/(\d+)$", self.path.split("?", 1)[0])
        if not m:
            return self._send(400, {"error": "need /api/limits/<id>"})
        n = get_store().delete(int(m.group(1)))
        self._send(200 if n else 404, {"deleted": n})

    # silence default logging
    def log_message(self, *a, **k):
        pass

_state = {"last": {}, "streak": 0}

def get_view_bypass():
    """Latest detection + effective nuclear state, JSON-able."""
    s = get_store()
    now = E.now_ms()
    return {
        "detected": _state["last"],
        "nuclear": s.nuclear,
        "effective_nuclear": s.is_nuclear() and not s.reward_active(now),
        "reason": s.nuclear_reason,
        "override": s.override,
        "reward": {
            "active": s.reward_active(now),
            "left_ms": int(max(0, (s.reward_until or 0) - now)),
        },
    }

# ---------------- background ticker ----------------
def ticker(store, interval=5.0, lock_every=3600):
    """Tick the limits engine, and re-lock non-Firefox browsers once per
    lock_every seconds via tools/lock_browsers.sh (needs the one-time
    passwordless-sudo setup: sudo bash tools/enable_hourly_lock.sh).
    A lock failure is logged and retried on the next hour; the gateway's DNS
    duty never depends on it."""
    lock_script = os.path.join(BASE, "tools", "lock_browsers.sh")
    last_lock = 0.0
    while True:
        try:
            store.tick(E.now_ms())
        except Exception:
            pass
        if os.path.exists(lock_script) and time.time() - last_lock >= lock_every:
            last_lock = time.time()
            try:
                r = subprocess.run(["sudo", "-n", lock_script],
                                   capture_output=True, text=True, timeout=180)
                out = ((r.stdout or "") + ("\n" + (r.stderr or ""))).strip()
                if r.returncode != 0:
                    print("[lock] hourly browser relock FAILED rc=%d: %s "
                          "(one-time setup: sudo bash tools/enable_hourly_lock.sh)"
                          % (r.returncode, out[-200:]))
                else:
                    tail = [l.strip() for l in out.splitlines() if l.strip()]
                    print("[lock] hourly browser relock ok: %s" % (tail[-1] if tail else "done"))
            except Exception as e:
                print("[lock] browser relock error:", e)
        time.sleep(interval)

# ---------------- bypass watcher ----------------
def bypass_watcher(store, lan_gw, doh_port, interval=5.0, uncontrolled_streak=3):
    """Polls the machine once per interval:
      * VPN up (tun iface / client process / gateway changed?)
      * traffic to PUBLIC DoH endpoints (someone re-enabled DoH to a provider)
      * Firefox running but NOT resolving via our DoH endpoint (DoH switched
        off -> plain OS DNS -> we can't see anything)
      * the profile's DNS-lock file (user.js) removed/edited
    Any hit arms nuclear mode (sinkhole answers 0.0.0.0 for EVERYTHING except
    localhost) until the cause is gone or the parent overrides."""
    was_nuclear = False
    streak = 0
    while True:
        try:
            d = B.detect(lan_gateway=lan_gw, doh_port=doh_port)
            lock_ok, lock_why = profile_lock_ok()

            nuclear, reason = False, None
            if d["vpn"]["active"]:
                nuclear, reason = True, ("VPN detected: %s" % d["vpn"]["why"])
            elif d["public_doh"]["active"]:
                nuclear, reason = True, ("Public DoH in use (bypass!): %s" % ", ".join(d["public_doh"]["peers"]))
            elif not lock_ok:
                nuclear, reason = True, ("Firefox DNS lock tampered: %s" % lock_why)
            else:
                # Only arm on POSITIVE evidence of off-gateway DNS from Firefox
                # (sockets to the :53 OS resolver or to public DoH :443). An idle
                # browser simply has no open sockets and must not be accused.
                off = d.get("off_dns") or {"active": False, "peers": []}
                if off["active"]:
                    streak += 1
                    if streak >= 2:
                        nuclear, reason = True, ("Firefox is resolving OUTSIDE the gateway "
                                                 "(OS DNS/VPN/public DoH): %s" % ", ".join(off["peers"]))
                else:
                    streak = 0

            _state["last"] = d
            if store.is_nuclear() != (nuclear and not store.override):
                pass
            store.set_nuclear(nuclear, reason)
            eff = store.nuclear and not store.override
            if eff != was_nuclear:
                print(("[!] BYPASS -> nuclear ON: %s" % reason) if eff
                      else "[+] nuclear OFF (bypass cleared)")
                was_nuclear = eff
        except Exception as e:
            print("[!] watcher error:", e)
        time.sleep(interval)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DNS_PORT", "5354")),
                    help="plain-DNS sinkhole port (UDP+TCP)")
    ap.add_argument("--web", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--uphost", default=os.environ.get("DNS_UP", "127.0.0.53"))
    ap.add_argument("--upport", type=int, default=53)
    ap.add_argument("--dohport", type=int, default=int(os.environ.get("DOH_PORT", "8443")),
                    help="DoH (TLS) responder port — the browser front-door (0 = off)")
    ap.add_argument("--certdir", default=os.path.join(BASE, "certs"))
    ap.add_argument("--lan-gw", default=os.environ.get("LAN_GW", "192.168.0.1"),
                    help="expected LAN default gateway (VPN detection baseline)")
    ap.add_argument("--bypass-interval", type=float, default=5.0,
                    help="bypass watch interval in seconds")
    ap.add_argument("--bypass-protect", dest="bypass_protect", action="store_true",
                    help="(root) apply iptables DoH/VPN-drop + DNS-redirect rules via bypass.py")
    ap.add_argument("--bypass-dry-run", dest="bypass_dry_run", action="store_true",
                    help="with --bypass-protect, print rules instead of applying (no root needed)")
    ap.add_argument("--vpn", default=os.environ.get("BYPASS_VPN", ""),
                    help="comma-separated VPN peer IPs to block (auto-detected if omitted)")
    ap.add_argument("--db", default=None, help="sqlite db path")
    ap.add_argument("--no-sinkhole", dest="no_sinkhole", action="store_true",
                    help="(central mode) do not bind the plain-DNS sinkhole")
    ap.add_argument("--no-doh", dest="no_doh", action="store_true",
                    help="(central mode) do not start the DoH (TLS) front-door")
    ap.add_argument("--no-bypass", dest="no_bypass", action="store_true",
                    help="(central mode) skip the device-local bypass/nuclear watcher")
    args = ap.parse_args()
    upstream = (args.uphost, args.upport)

    store = get_store(args.db)
    # Restore an in-progress/in-active workout reward window across restarts
    # (the window itself lives in data/workout.json; the in-memory flag does
    # not, and without this a restart would re-block everything mid-pass).
    try:
        _wst = get_workout().status()
        if _wst.get("reward_until_ms"):
            store.set_reward_until(_wst["reward_until_ms"])
            if _wst["reward_active"]:
                print("[+] workout no-block window restored until "
                      + time.strftime("%H:%M:%S", time.localtime(_wst["reward_until_ms"] / 1000.0)))
    except Exception as e:
        print("[!] workout restore failed:", e)
    threading.Thread(target=ticker, args=(store,), daemon=True).start()

    # --- (root) iptables layer: only runs when explicitly requested ---
    # (irrelevant in central mode, but honoured if someone runs single-host)
    if args.bypass_protect:
        vpn = [x for x in (args.vpn or "").split(",") if x]
        if not vpn:
            vpn = B.detect_vpn_peers()
        ruleset, tables = B.generate_rules(args.uphost, args.port, vpn_ips=vpn)
        _dry = args.bypass_dry_run or not B.is_root()
        ok, msg = B.apply(ruleset, dry_run=_dry)
        mode = "dry-run (print-only; needs sudo to apply)" if _dry else ("APPLIED" if ok else "FAILED")
        print("[+] bypass-protection [%s] tables=%s" % (mode, ",".join(tables)))
        if _dry:
            print(msg)
        elif not ok:
            print("[!] %s" % msg)
        if not _dry:
            _snap = B.snapshot()[0]
            def _rollback(signum, frame=None):
                B.rollback(_snap)
                sys.exit(0)
            signal.signal(signal.SIGTERM, _rollback)
            signal.signal(signal.SIGINT, _rollback)

    # --- the front doors (skipped in central-only mode) ---
    dns_listener = None
    if not args.no_sinkhole:
        dns_listener, _threads = DNS.serve(store, port=args.port, upstream=upstream)
        print("[+] plain-DNS sinkhole listening on :%d (upstream %s:%d)" % (args.port, *upstream))
    doh_httpd = None
    if args.dohport and not args.no_doh:
        if DOH.tls_available(args.certdir):
            doh_httpd = DOH.serve(store, port=args.dohport, upstream=upstream, certdir=args.certdir)
            print("[+] DoH front-door on https://127.0.0.1:%d/dns-query "
                  "(point Firefox at it: tools/setup_firefox.sh)" % args.dohport)
        else:
            print("[!] No TLS certs in %s — DoH front-door DISABLED "
                  "(run tools/gen_certs.sh)." % args.certdir)

    if not args.no_bypass:
        threading.Thread(
            target=bypass_watcher,
            args=(store, args.lan_gw, args.dohport or 8443, args.bypass_interval),
            daemon=True, name="bypass-watcher").start()

    httpd = ThreadingHTTPServer(("0.0.0.0", args.web), Handler)
    print("[+] ControlLimits: limits=%d" % len(store.snapshot()))
    if dns_listener is None:
        print("[+] CENTRAL-ONLY MODE (no sinkhole/DoH): serving admin+API only")
    print("[] HTTP admin+frontend on http://0.0.0.0:%d" % args.web)
    httpd.serve_forever()

if __name__ == "__main__":
    main()
