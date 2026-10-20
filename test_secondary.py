import sys, os, time, subprocess, socket, json, struct, ssl, time
import http.client as httplib
import dns as DNS
from remote_store import RemoteStore

BASE = "/home/pietro/limiter"
DB = "/tmp/sec_limits.db"; CACHE = "/tmp/sec_cache.db"; CACHE2 = "/tmp/sec_cache2.db"
WEB1 = 8082; WEB2 = 8083; DOH2 = 8444; UPPORT = 8450

def cleanup():
    for f in [DB, DB+"-wal", DB+"-shm", CACHE, CACHE+"-wal", CACHE+"-shm",
              CACHE2, CACHE2+"-wal", CACHE2+"-shm"]:
        if os.path.exists(f): os.remove(f)
cleanup()

def popen(port):
    s = socket.socket(); s.settimeout(0.5)
    try: s.connect(("127.0.0.1", port)); return True
    except Exception: return False
    finally: s.close()

def free_port(port):
    """Kill any stray process already bound to `port` (e.g. a previous run that
    didn't clean up) so our fresh process can bind it."""
    import signal
    inode = None
    try:
        for line in open("/proc/net/tcp").read().splitlines()[1:]:
            p = line.split()
            if len(p) < 10 or p[3] != "0A": continue
            ip, ph = p[1].rsplit(":")
            if int(ph, 16) == port:
                inode = p[9]; break
    except Exception: pass
    if inode is None: return
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                try:
                    if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == "socket:[%s]" % inode:
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                        except OSError:
                            pass
                        print("   [free] killed stray pid %s on :%d" % (pid, port))
                        break
                except OSError: pass
        except OSError: pass

def kill(p):
    try: p.terminate(); p.wait(timeout=3)
    except Exception:
        try: p.kill()
        except Exception: pass

def make_stub():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", UPPORT)); s.settimeout(0.5)
    def loop():
        while True:
            try: d, a = s.recvfrom(4096)
            except socket.timeout: continue
            except OSError: break
            try:
                parsed = DNS.parse_query(d)
                if not parsed: continue
                tid, qs = parsed
                an = sum(1 for _,qt,_ in qs if qt==1)
                resp = struct.pack(">HHHHHH", tid, 0x8180, len(qs), an, 0, 0)
                for _,_,qraw in qs: resp += qraw
                if an > 0:
                    resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 30, 4) + socket.inet_aton("1.2.3.4")
                s.sendto(resp, a)
            except Exception: pass
    threading = __import__("threading")
    threading.Thread(target=loop, daemon=True).start()
    return s

stub = make_stub(); time.sleep(0.2)
ok = True
def check(name, cond):
    global ok; print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

def api(port, method, path, body=None):
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type":"application/json"} if data else {}
    req = Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method, headers=hdr)
    try:
        r = urlopen(req, timeout=5); return r.getcode(), r.read().decode()
    except HTTPError as e: return e.code, e.read().decode()

proc_central = subprocess.Popen([sys.executable, "app.py", "--web", str(WEB1),
    "--dohport", "0", "--no-sinkhole", "--no-doh", "--no-bypass", "--db", DB],
    stdout=subprocess.DEY if False else subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    free_port(WEB1)  # avoid stale central collisions
    time.sleep(0.5)
    for _ in range(50):
        if popen(WEB1): break
        time.sleep(0.2)
    check("central up", popen(WEB1))
    api(WEB1, "POST", "/api/limits", {"name":"prom","type":"absolute","domains":["prom.ua"]})
    api(WEB1, "POST", "/api/limits", {"name":"free","type":"time","domains":["social.site"],"grace_min":40,"cooldown_min":120})

    free_port(WEB2); free_port(DOH2); free_port(UPPORT)
    time.sleep(0.3)
    proc_sec = subprocess.Popen([sys.executable, "secondary.py", "--central", "http://127.0.0.1:%d" % WEB1,
        "--web", str(WEB2), "--dohport", str(DOH2), "--certdir", os.path.join(BASE,"certs"),
        "--uphost", "127.0.0.1", "--upport", str(UPPORT), "--ttl", "30"],
        cwd=BASE, stdout=subprocess.DEY if False else subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(60):
        if popen(WEB2): break
        time.sleep(0.2)
    check("secondary UI up", popen(WEB2))

    st, body = api(WEB2, "GET", "/api/remote/status")
    status = json.loads(body)
    print("   [debug] num_limits=%s online=%s emergency=%s nuclear=%s central=%s"
          % (status.get("num_limits"), status.get("online"), status.get("emergency"),
             status.get("nuclear"), status.get("central")))
    check("secondary synced (online)", status.get("online"))
    check("secondary not nuclear", not status.get("effective_nuclear"))
    check("secondary sees 2 limits", status.get("num_limits") == 2)

    from doh import build_wire_query
    cert = os.path.join(BASE, "certs", "ca.pem")
    def doh_query(name):
        wire = build_wire_query(name)
        ctx = ssl.create_default_context(cafile=cert); ctx.check_hostname = False
        hc = httplib.HTTPSConnection("127.0.0.1", DOH2, context=ctx)
        hc.request("POST", "/dns-query", body=wire, headers={"Content-Type":"application/dns-message"})
        resp = hc.getresponse(); return resp.read()

    blocked = doh_query("prom.ua")
    check("prom.ua blocked (00 04 00 00 00 00 sentinel)", blocked.endswith(b"\x00\x04\x00\x00\x00\x00"))
    free = doh_query("example.org")
    check("example.org forwarded (real 1.2.3.4 from upstream)", b"\x01\x02\x03\x04" in free)

    rs = RemoteStore(CACHE2, "http://127.0.0.1:%d" % WEB1, ttl_seconds=30, sync_now=False)
    n = rs.report_access("social.site")
    check("report_access arms grace centrally", n >= 1)

    st, body = api(WEB2, "GET", "/api/limits")
    lim = json.loads(body)
    check("child /api/limits returns both limits", len(lim) == 2 and all("updated_at" in l for l in lim))
finally:
    kill(proc_sec); kill(proc_central)
    try: stub.close()
    except Exception: pass
    cleanup()

print("ALL SECONDARY E2E TESTS PASSED" if ok else "SOME SECONDARY E2E TESTS FAILED")
