"""Work / focus-session feature tests (unit + live integration).

Covers:
  * the Work state machine (work.py): monotonic limit/budget, success on
    reaching the minimum, failure+penalty when the timer runs out, and
    cheat-resistance (client-posted deltas capped by real elapsed time).
  * integration against a live single-host app.py: the block-eom-thing
    behaviour — a normally-FREE domain is blocked while a work session is
    active (and un-blocked after), plus the penalty window.

Run with the gateway stopped so :5354 / :8080 are free (see README §8).
"""
import sys, os, time, socket, struct, subprocess, shutil, json, urllib.request

BASE = "/home/pietro/limiter"
sys.path.insert(0, BASE)
# small floors/timers so the cheat-resistant (real-elapsed-capped) tick loop is
# fast in the unit tests without waiting real minutes.
os.environ["MIN_WORK_BUDGET_MS"] = "1000"
os.environ["MIN_WORK_MIN_MS"] = "500"
os.environ["WORK_BUDGET_MS"] = "6000"
os.environ["WORK_MIN_MS"] = "2000"
os.environ["WORK_PALTY_MS"] = "2000"
import work as WK
import dns as DNS

WORK_DB = "/tmp/test_work_limits.db"
WORK_FILE = "/tmp/test_work.json"
WORK_PALTY_FILE = "/tmp/test_work_palty.json"

def rm(p):
    for f in (p, p+"-wal", p+"-shm"):
        if os.path.exists(f): os.remove(f)

def rm_workfile():
    for f in (WORK_FILE, WORK_FILE+".tmp"):
        if os.path.exists(f): os.remove(f)

# ---------------------------------------------------------------------------
# unit tests (state machine only)
# ---------------------------------------------------------------------------
def unit_tests():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

    rm_workfile()
    w = WK.Work(WORK_FILE)

    # first card establishes the common minimum (floor), and it is monotonic
    s = w.add_card("Email", "communication")
    check("add_card creates a card + establishes limit",
          len(s["cards"]) == 1 and s["cards"][0]["kind"] == "communication"
          and s["limit_ms"] >= WK.MIN_MIN_MS)

    # limit can only grow, never shrink
    s = w.set_limit(s["limit_ms"] - 1)            # try to LOWER -> ignored
    check("set_limit monotonic (lower ignored)", s["limit_ms"] >= WK.MIN_MIN_MS)
    newlim = s["limit_ms"] + 5000
    s = w.set_limit(newlim)
    check("set_limit can raise", s["limit_ms"] == newlim)

    # budget monotonic too
    s = w.set_budget(s["budget_ms"] - 1)
    check("set_budget monotonic (lower ignored)", s["budget_ms"] >= WK.MIN_BUDGET_MS)
    s = w.set_budget(WK.MIN_BUDGET_MS)            # floor it

    # ---- success path ---- (budget default 6s > limit default 2s -> min first)
    s = w.start()
    check("start -> session working", s["session"] == "working" and s["work_until_ms"] > time.time()*1000)
    # cheat-resistance: a huge claimed delta is capped by REAL elapsed time
    time.sleep(0.05)
    s, err = w.tick(10_000_000)
    check("tick cheat-resistant (accrued << claimed delta)", err is None and s["accrued_ms"] < 5000)
    # keep crediting until the minimum is reached (real time)
    _t = 0
    while s["session"] != "success" and _t < 30:
        time.sleep(0.1); _t += 0.1
        s, err = w.tick(10_000_000)               # huge delta, capped by real elapsed
    check("success after reaching the minimum", s["session"] == "success" and s["accrued_ms"] >= s["limit_ms"])
    check("success clears the block window", s["work_until_ms"] == 0)

    # ---- failure path + penalty (budget 6s < limit 7s -> timer runs out first) ----
    rm_workfile()
    w = WK.Work(WORK_FILE)
    w.add_card("Task", "task")
    w.set_limit(7000)                               # 7s minimum (> budget)
    w.set_budget(6000)                              # 6s budget < minimum
    s = w.start()
    _t = 0
    while s["session"] != "failed" and _t < 30:
        time.sleep(0.1); _t += 0.1
        s, err = w.tick(10_000_000)               # huge delta, capped by real elapsed
    check("failure when timer runs out first", s["session"] == "failed")
    check("failure arms a penalty window", s["penalty_active"] and s["penalty_left_ms"] > 0)
    check("failed status keeps the block window armed", s["work_until_ms"] > time.time()*1000)

    # pause/resume don't accrue while paused
    rm_workfile()
    w = WK.Work(WORK_FILE)
    w.add_card("T","task"); w.set_limit(60*60*1000); w.set_budget(60*60*1000)
    w.start()
    time.sleep(0.2); s = w.pause()
    a1 = s["accrued_ms"]
    time.sleep(0.3); s = w.status()
    check("pause does not accrue", s["accrued_ms"] == a1 and s["session"] == "paused")
    s = w.resume(); time.sleep(0.2); s, _err = w.tick(10_000_000)
    check("resume accrues again", s["accrued_ms"] > a1)

    rm(WORK_FILE); rm_workfile()
    return ok

# ---------------------------------------------------------------------------
# integration tests (live app.py over the real DNS resolver)
# ---------------------------------------------------------------------------
def http(method, path, body=None, base="http://127.0.0.1:8080"):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        r = urllib.request.urlopen(req, timeout=5); return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def build_query(name, tid=0x5555):
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0); lab = b""
    for p in name.split("."): lab += bytes([len(p)]) + p.encode()
    return q + lab + b"\x00" + struct.pack(">HH", 1, 1)

def dns_query(name, port=5358):  # 5358 = app plain-DNS sinkhole (query target)
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.bind(("127.0.0.1", 0)); c.settimeout(3)
    c.sendto(build_query(name), ("127.0.0.1", port))
    resp, _ = c.recvfrom(4096); c.close()
    tid, flags, zd, an, ns, ar = struct.unpack(">HHHHHH", resp[:12])
    off = 12
    for _ in range(zd):
        while True:
            L = resp[off]
            if L == 0: off += 1; break
            if L & 0xc0: off += 2; break
            off += 1 + L
        off += 4
    for _ in range(an):
        if resp[off] == 0xc0: off += 2
        else:
            while True:
                L = resp[off]
                if L == 0: off += 1; break
                if L & 0xc0: off += 2; break
                off += 1 + L
        typ, cls, ttl, rdlen = struct.unpack(">HHIH", resp[off:off+10]); off += 10
        ip = resp[off:off+rdlen]; off += rdlen
        if typ == 1 and rdlen == 4: return ".".join(str(b) for b in ip)
    return None

def stub():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 5354))
    def loop():
        while True:
            try: d, a = s.recvfrom(4096)
            except socket.timeout: continue
            except OSError: break
            parsed = DNS.parse_query(d)
            if not parsed: continue
            tid, qs = parsed
            an = sum(1 for _,qt,_ in qs if qt == 1)
            resp = struct.pack(">HHHHHH", tid, 0x8180, len(qs), an, 0, 0)
            for _,_,qraw in qs: resp += qraw
            if an > 0:
                resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 30, 4) + socket.inet_aton("9.9.9.9")
            try: s.sendto(resp, a)
            except OSError: break
    threading.Thread(target=loop, daemon=True).start()
    return s

import threading
def integration_tests():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

    rm(WORK_DB); rm_workfile()
    env = os.environ.copy()
    env["MIN_WORK_BUDGET_MS"] = "1000"
    env["MIN_WORK_MIN_MS"] = "500"
    env["WORK_BUDGET_MS"] = "6000"     # 6s budget
    env["WORK_MIN_MS"] = "2000"        # 2s minimum
    env["WORK_PALTY_MS"] = "2000"      # 2s penalty
    env["WORK_FILE"] = WORK_FILE

    # stub = upstream (5354): answers 9.9.9.9 for any A query.
    # app sinkhole is the query target (5358); it enforces limits + work block and
    # forwards allowed queries to the stub upstream.
    stub(); time.sleep(0.2)
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "app.py"),
        "--port", "5358", "--web", "8080", "--uphost", "127.0.0.1", "--upport", "5354",
        "--db", WORK_DB, "--no-doh", "--no-bypass"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                if http("GET", "/api/health")[1].get("ok"): break
            except Exception: pass
            time.sleep(0.2)

        # pages + endpoints exist
        code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",
            "http://127.0.0.1:8080/work.html"], capture_output=True, text=True).stdout
        check("/work.html served (200)", code == "200")
        body = subprocess.run(["curl","-s","http://127.0.0.1:8080/work.html"], capture_output=True, text=True).stdout
        check("/work.html is the Work page", "Work" in body and "Start Work" in body)
        check("GET /api/work has fields", "budget_ms" in http("GET","/api/work")[1])

        # add an absolute limit + a free time-limit
        http("POST", "/api/limits", {"name": "prom", "type": "absolute", "domains": ["prom.ua"]})
        http("POST", "/api/limits", {"name": "free", "type": "time", "domains": ["free.ua"],
            "grace_min": 40, "cooldown_min": 120})

        # baseline: free.ua (free state) forwards
        check("free.ua forwards BEFORE work (9.9.9.9)", dns_query("free.ua") == "9.9.9.9")
        check("prom.ua blocked BEFORE work (0.0.0.0)", dns_query("prom.ua") == "0.0.0.0")

        # create a card + start a work session -> should block everything
        http("POST", "/api/work", {"action": "add_card", "name": "Task", "kind": "task"})
        s = http("POST", "/api/work", {"action": "start"})[1]
        check("start arms the block window", s["session"] == "working" and s["work_until_ms"] > time.time()*1000)
        check("free.ua BLOCKED during work session (0.0.0.0)", dns_query("free.ua") == "0.0.0.0")

        # tick to reach the minimum -> success -> unblock
        _t = 0
        while s["session"] != "success" and _t < 40:
            time.sleep(0.1); _t += 0.1
            s = http("POST", "/api/work", {"action": "tick", "delta_ms": 10_000_000})[1]
        check("session reaches success over HTTP", s["session"] == "success")
        check("free.ua FORWARDS again after success (un-blocked)", dns_query("free.ua") == "9.9.9.9")

        # failure + penalty path: tiny budget, large minimum
        rm_workfile()
        # restart so the fresh tiny-budget/large-min config applies
        proc.terminate()
        try: proc.wait(timeout=3)
        except Exception: proc.kill()
        env2 = env.copy()
        # failure path: budget floor (1s) BELOW the minimum floor (60s), so a fresh
        # session can never accrue the 60s minimum before its 1s budget runs out.
        env2["MIN_WORK_BUDGET_MS"] = "1000"   # budget floor 1s
        env2["MIN_WORK_MIN_MS"] = "60000"     # minimum floor 60s (> budget)
        env2["WORK_BUDGET_MS"] = "1000"       # 1s budget
        env2["WORK_MIN_MS"] = "60000"         # 60s minimum (never reached)
        env2["WORK_PALTY_MS"] = "2000"        # 2s penalty
        proc = subprocess.Popen([sys.executable, os.path.join(BASE, "app.py"),
            "--port", "5358", "--web", "8080", "--uphost", "127.0.0.1", "--upport", "5354",
            "--db", WORK_DB, "--no-doh", "--no-bypass"],
            env=env2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                if http("GET", "/api/health")[1].get("ok"): break
            except Exception: pass
            time.sleep(0.2)

        http("POST", "/api/work", {"action": "add_card", "name": "Task", "kind": "task"})
        http("POST", "/api/work", {"action": "start"})
        _t = 0
        while True:
            time.sleep(0.1); _t += 0.1
            s = http("POST", "/api/work", {"action": "tick", "delta_ms": 10_000_000})[1]
            if s["session"] == "failed": break
            if _t > 40: break
        check("session FAILS when timer runs out first", s["session"] == "failed")
        check("penalty active after failure (everything still blocked)",
              s.get("penalty_active") and dns_query("free.ua") == "0.0.0.0")

        # wait for penalty to expire, then it should unblock
        _t = 0
        while _t < 40:
            s = http("GET", "/api/work")[1]
            if not s["penalty_active"]: break
            time.sleep(0.1); _t += 0.1
        check("penalty clears after its window", not s["penalty_active"])
        time.sleep(0.2)
        check("free.ua FORWARDS again after penalty expires", dns_query("free.ua") == "9.9.9.9")
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except Exception: proc.kill()
        rm(WORK_DB); rm_workfile()
    return ok


if __name__ == "__main__":
    u = unit_tests()
    i = integration_tests()
    print("ALL WORK TESTS PASSED" if (u and i) else "SOME WORK TESTS FAILED")
    sys.exit(0 if (u and i) else 1)
