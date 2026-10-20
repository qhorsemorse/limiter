import sys, os, time, subprocess, socket, shutil, json

BASE = "/home/pietro/limiter"
sys.path.insert(0, BASE)

DB = "/tmp/test_remote_limits.db"
CACHE = "/tmp/test_remote_cache.db"
for f in [DB, DB + "-wal", DB + "-shm", CACHE, CACHE + "-wal", CACHE + "-shm"]:
    if os.path.exists(f): os.remove(f)

WEB = 8081
CENTRAL = "http://127.0.0.1:%d" % WEB

def http(method, path, body=None):
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    req = Request(Centralize(path), data=data, method=method, headers=hdr)
    try:
        r = urlopen(req, timeout=5); return r.getcode(), json.loads(r.read().decode() or "{}")
    except HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def Centralize(path):
    return CENTRAL + path if not path.startswith("http") else path

def port_open(port):
    s = socket.socket(); s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port)); return True
    except Exception:
        return False
    finally:
        s.close()

proc = subprocess.Popen(
    [sys.executable, os.path.join(BASE, "app.py"),
     "--port", "5355", "--web", str(WEB), "--dohport", "0",
     "--no-sinkhole", "--no-doh", "--no-bypass", "--db", DB],
    stdout=subprocess.DEY if False else subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)

ok = True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

try:
    for _ in range(50):
        try:
            if port_open(WEB): break
        except Exception: pass
        time.sleep(0.2)
    check("central web up on :%d" % WEB, port_open(WEB))

    # admin adds limits on the CENTRAL
    st, j = http("POST", "/api/limits", {"name": "prom", "type": "absolute", "domains": ["prom.ua"]})
    check("central add absolute -> 201", st == 201)
    st, j = http("POST", "/api/limits", {"name": "social", "type": "time", "domains": ["social.site"],
                                          "grace_min": 40, "cooldown_min": 120})
    check("central add time -> 201", st == 201)

    from remote_store import RemoteStore

    # --- cache-first sync ---
    store = RemoteStore(CACHE, CENTRAL, ttl_seconds=30.0)
    check("sync succeeded (online)", store.online and not store.emergency)
    snap = store.snapshot()
    check("cache copied 2 limits", len(snap) == 2)
    check("cache rows carry updated_at", all(l.get("updated_at") for l in snap))

    # --- blocked by absolute limit, from cache ---
    blocked, lid = store.evaluate("prom.ua")
    check("prom.ua blocked (absolute)", blocked and lid is not None)
    blocked, lid = store.evaluate("www.prom.ua")
    check("www.prom.ua blocked (subdomain)", blocked)

    # --- free site forwards while online ---
    blocked, lid = store.evaluate("example.org")
    check("example.org free -> forwards online", not blocked)

    # --- detect arms grace centrally, cached state reflects it ---
    n = store.detect("social.site")
    check("detect reported to central (>=1 affected)", n >= 1)
    social = [l for l in store.snapshot() if l["name"] == "social"][0]
    check("grace armed centrally after detect", social["grace_until"] is not None)

    # --- TTL / staleness: low ttl forces re-sync when central changes ---
    store2 = RemoteStore(CACHE, CENTRAL, ttl_seconds=0.3)
    check("fresh sync ttl small", store2.online)
    # add a NEW limit on the central while store2's cache is still fresh
    http("POST", "/api/limits", {"name": "newabs", "type": "absolute", "domains": ["new.block"]})
    # immediately (within ttl) cache is fresh -> new limit not yet visible
    time.sleep(0.05)
    _b, _ = store2.evaluate("new.block")
    # force a stale evaluation: push last_sync far enough in the past, then evaluate
    store2.last_sync_ms = 0.0
    _b, lid = store2.evaluate("new.block")
    check("stale cache re-fetched from central -> new limit visible", lid is not None)

    # --- OFFLINE / emergency ---
    # point a store at an unreachable central
    offline = RemoteStore(CACHE, "http://127.0.0.1:1", ttl_seconds=30.0, sync_now=False)
    _ = offline.sync()  # attempt fails
    check("offline -> emergency", not offline.online and offline.emergency)
    b_abs, _ = offline.evaluate("prom.ua")
    check("offline: absolute lock still BLOCKED", b_abs)
    b_free, _ = offline.evaluate("example.org")
    check("offline: free site fail-closed (blocked)", b_free)

    # allow_free_offline overrides the fail-closed default
    lo = RemoteStore(CACHE, "http://127.0.0.1:1", ttl_seconds=30.0,
                     sync_now=False, allow_free_offline=True)
    _ = lo.sync()
    b_free2, _ = lo.evaluate("example.org")
    check("offline allow_free_offline -> free site forwards", not b_free2)
    b_abs2, _ = lo.evaluate("prom.ua")
    check("offline allow_free_offline: absolute lock STILL blocked", b_abs2)

    # report_access while offline returns 0 (can't reach central) and no crash
    check("report_access offline -> 0", offline.report_access("social.site") == 0)

finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()
    for f in [DB, DB + "-wal", DB + "-shm", CACHE, CACHE + "-wal", CACHE + "-shm"]:
        if os.path.exists(f): os.remove(f)

print("ALL REMOTE STORE TESTS PASSED" if ok else "SOME REMOTE STORE TESTS FAILED")
