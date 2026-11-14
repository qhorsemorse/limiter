"""Work / focus-session feature tests (unit + live integration) — pomodoro goal.

Covers the v7 concept:
  * the Work state machine (work.py): settable DAILY GOAL + work/rest pomodoro
    stretches; only work-stretch time credits the goal; the resolver's block
    window is ON during work stretches and OFF during rests; success on
    reaching the goal; a break ending in the WAITING state (the next work
    stretch does NOT auto-start — only resume/pause act on it); a day ending
    with the goal unmet is LOGGED and the new day starts IDLE (no failure
    state, no penalty, no self-lock of the timer); cheat-resistance
    (client-posted deltas capped by real elapsed time); and the persisted
    per-day work-data log.
  * integration against a live single-host app.py: a normally-FREE domain is
    blocked during a work stretch, un-blocked during the rest stretch, stays
    un-blocked while the timer waits for the user, is blocked again once work
    is resumed, and stays un-blocked after success. A legacy stuck "failed"
    state heals to idle (start works, nothing blocked by a penalty).

Run with the gateway stopped so :5354 / :8080 are free (see README §8).
"""
import sys, os, time, socket, struct, subprocess, json, urllib.request

BASE = "/home/pietro/limiter"
sys.path.insert(0, BASE)
# small goals/stretches so the cheat-resistant (real-elapsed-capped) tick loop
# is fast in the unit tests without waiting real minutes.
os.environ["MIN_WORK_GOAL_MS"] = "500"
os.environ["WORK_GOAL_MS"] = "3000"        # default goal 3 s
os.environ["WORK_INTERVAL_MS"] = "2000"    # default work stretch 2 s
os.environ["WORK_REST_MS"] = "1000"        # default rest stretch 1 s
os.environ["MIN_WORK_INTERVAL_MS"] = "100"
import work as WK
import dns as DNS

WORK_DB = "/tmp/test_work_limits.db"
WORK_FILE = "/tmp/test_work.json"

def rm(p):
    for f in (p, p+"-wal", p+"-shm"):
        if os.path.exists(f): os.remove(f)

def rm_workfile():
    for f in (WORK_FILE, WORK_FILE+".tmp"):
        if os.path.exists(f): os.remove(f)

NOW = lambda: time.time()*1000

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

    # setpoints: goal freely adjustable (clamped), intervals settable
    s = w.add_card("Email", "communication")
    check("add_card creates a card", len(s["cards"]) == 1 and s["cards"][0]["kind"] == "communication")
    check("status exposes goal + intervals",
          s["goal_ms"] >= WK.MIN_GOAL_MS and s["work_interval_ms"] > 0 and s["rest_interval_ms"] > 0)
    s = w.set_goal(4000); check("set_goal raises the goal", s["goal_ms"] == 4000)
    s = w.set_goal(1000); check("set_goal can LOWER the goal (settable, not monotonic)", s["goal_ms"] == 1000)
    s = w.set_goal(1); check("goal clamps to floor", s["goal_ms"] == WK.MIN_GOAL_MS)
    s = w.set_goal(10**12); check("goal clamps to 8h cap", s["goal_ms"] == WK.MAX_MS)
    s = w.set_goal(4000)
    s = w.set_interval(work_ms=3000, rest_ms=1500)
    check("set_interval work+rest", s["work_interval_ms"] == 3000 and s["rest_interval_ms"] == 1500)
    s = w.set_interval(rest_ms=0); check("rest stretch may be 0", s["rest_interval_ms"] == 0)
    s = w.set_interval(work_ms=1, rest_ms=1500)
    check("work stretch clamps to floor", s["work_interval_ms"] == WK.MIN_WORK_MS)
    s = w.set_interval(work_ms=3000, rest_ms=1500)   # the stretch lengths for the run below

    # ---- start: working, work stretch armed, block ON ----
    s = w.start()
    check("start -> working (work stretch)",
          s["session"] == "working" and s["phase"] == "work"
          and s["work_until_ms"] > NOW() and s["phase_left_ms"] > 2500)
    # cheat-resistance: a huge claimed delta is capped by REAL elapsed time
    time.sleep(0.05)
    s, err = w.tick(10_000_000)
    check("tick cheat-resistant (accrued << claimed delta)", err is None and s["accrued_ms"] < 500)

    # work stretch (3 s) ends -> RESTING: block OFF, rest starts accumulating
    _t = 0
    while s["session"] != "resting" and _t < 20:
        time.sleep(0.2); _t += 0.2
        s, err = w.tick(10_000_000)
    check("work stretch ends -> rest stretch", s["session"] == "resting" and s["phase"] == "rest")
    check("rest stretch clears the block window", s["work_until_ms"] == 0)
    a_work = s["accrued_ms"]
    check("work time credited ~= one work stretch", 2800 <= a_work <= 3600)
    time.sleep(0.5); s, err = w.tick(10_000_000)
    check("rest minutes accumulate (but don't credit the goal) while the goal is frozen",
          s["rested_ms"] >= 400 and s["accrued_ms"] == a_work)

    # rest stretch (1.5 s) ends -> WAITING: the work stretch does NOT auto-start
    _t = 0
    while s["session"] != "waiting" and _t < 20:
        time.sleep(0.2); _t += 0.2
        s, err = w.tick(10_000_000)
    check("rest stretch ends -> WAITING (no auto work restart)",
          s["session"] == "waiting" and s["awaiting_work"] is True and s["work_until_ms"] == 0)
    # resume -> fresh work stretch, block re-armed
    s = w.resume()
    check("resume from waiting starts a work stretch",
          s["session"] == "working" and s["cycles"] >= 1 and s["awaiting_work"] is False)
    check("block re-armed for the new work stretch", s["work_until_ms"] > NOW())

    # goal (4 s) reached during the 2nd work stretch -> SUCCESS, log written
    _t = 0
    while s["session"] != "success" and _t < 20:
        time.sleep(0.2); _t += 0.2
        s, err = w.tick(10_000_000)
    check("success after reaching the goal", s["session"] == "success" and s["complete"])
    check("success caps accrued at the goal", s["accrued_ms"] == 4000)
    check("success clears the block window", s["work_until_ms"] == 0)
    check("focused card carries the credited time",
          any(c["accrued_ms"] >= 3000 for c in s["cards"]))
    today = s["date"]
    check("work-data log stores the day", any(
        e["date"] == today and e["done"] and not e["failed"]
        and e["worked_ms"] == 4000 and e["rested_ms"] > 0 and e["cycles"] >= 1
        for e in s["log"]))
    check("JSON file persists the log", "log" in json.load(open(WORK_FILE)))

    # no restart of a done day
    s = w.start()
    check("start rejected after success", s["session"] == "success")

    # ---- pause semantics ----
    rm_workfile()
    w = WK.Work(WORK_FILE)
    w.set_interval(work_ms=5000, rest_ms=5000)
    w.set_goal(3600_000)
    w.start(); time.sleep(0.3)
    s = w.pause()
    a1 = s["accrued_ms"]; pl1 = s["phase_left_ms"]
    check("pause mid work stretch KEEPS the block", s["session"] == "paused" and s["work_until_ms"] > NOW())
    time.sleep(0.4); s = w.status()
    check("pause freezes credit", s["accrued_ms"] == a1)
    s = w.resume()
    check("resume back to work stretch", s["session"] == "working" and s["phase_left_ms"] <= pl1)
    time.sleep(0.3); s, _e = w.tick(10_000_000)
    check("resume accrues again", s["accrued_ms"] > a1)
    # into the rest stretch, then pause there -> block stays OFF
    _t = 0
    while s["session"] != "resting" and _t < 20:
        time.sleep(0.3); _t += 0.3
        s, _e = w.tick(10_000_000)
    s = w.pause()
    check("pause mid rest stretch keeps the block OFF", s["session"] == "paused" and s["work_until_ms"] == 0)
    s = w.resume(); time.sleep(0.2); s, _e = w.tick(10_000_000)
    check("resume from rest", s["session"] in ("resting", "working", "waiting"))

    # ---- waiting -> pause (silence) -> resume (work starts) ----
    rm_workfile()
    w = WK.Work(WORK_FILE)
    w.set_interval(work_ms=500, rest_ms=400)
    w.set_goal(3600_000)
    w.start()
    _t = 0
    while w.status()["session"] != "waiting" and _t < 40:
        time.sleep(0.15); _t += 0.15
        w.tick(10_000_000)
    check("cycle reaches WAITING after the rest", w.status()["session"] == "waiting")
    s = w.pause()
    check("pause while waiting -> silenced pause, block OFF, awaiting_work",
          s["session"] == "paused" and s["work_until_ms"] == 0 and s["awaiting_work"] is True)
    time.sleep(0.3)
    s = w.resume()
    check("resume from silenced waiting starts a work stretch",
          s["session"] == "working" and s["work_until_ms"] > NOW() and s["awaiting_work"] is False)
    s = w.pause()
    check("pause mid work stretch KEEPS the block (wait marker cleared)",
          s["session"] == "paused" and s["awaiting_work"] is False and s["work_until_ms"] > NOW())
    s = w.resume()
    check("normal resume back to work", s["session"] == "working")

    # ---- day end short of the goal: LOGGED, fresh IDLE day, timer NEVER locked ----
    rm_workfile()
    w = WK.Work(WORK_FILE)
    w.add_card("T", "task"); w.set_goal(4000); w.set_interval(work_ms=3000, rest_ms=0)
    s = w.start(); time.sleep(0.3); s, _e = w.tick(10_000_000)
    w.state["date"] = "1999-01-01"          # pretend midnight passed mid-session
    w._save()
    s = w.status()
    check("day ends short of the goal -> fresh IDLE day (no self-lock)",
          s["session"] == "idle" and s["work_until_ms"] == 0)
    check("short day logged in the work-data log (failed = stored data only)", any(
        e["date"] == "1999-01-01" and e["failed"] and not e["done"] and 0 < e["worked_ms"] < 4000
        for e in s["log"]))
    s = w.start()
    check("start works IMMEDIATELY after a short day (no penalty, no lock)",
          s["session"] == "working" and s["work_until_ms"] > NOW())

    rm_workfile()
    return ok

# ---------------------------------------------------------------------------
# integration tests (live app.py over the real DNS resolver)
# ---------------------------------------------------------------------------
# the test app gets its own web port so a live gateway on :8080 can keep running
APP_WEB = 8199

def http(method, path, body=None, base=None):
    base = base or ("http://127.0.0.1:%d" % APP_WEB)
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
    import threading
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

def start_app(env):
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "app.py"),
        "--port", "5358", "--web", str(APP_WEB), "--uphost", "127.0.0.1", "--upport", "5354",
        "--db", WORK_DB, "--no-doh", "--no-bypass"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            if http("GET", "/api/health")[1].get("ok"): break
        except Exception: pass
        time.sleep(0.2)
    return proc

def stop_app(proc):
    proc.terminate()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()

def integration_tests():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

    rm(WORK_DB); rm_workfile()
    env = os.environ.copy()
    env["WORK_GOAL_MS"] = "2000"        # 2 s of real work to reach
    env["WORK_INTERVAL_MS"] = "1500"    # 1.5 s work stretches
    env["WORK_REST_MS"] = "1000"        # 1 s rest stretches
    env["MIN_WORK_GOAL_MS"] = "500"
    env["MIN_WORK_INTERVAL_MS"] = "100"
    env["WORK_FILE"] = WORK_FILE

    # stub = upstream (5354): answers 9.9.9.9 for any A query.
    # app sinkhole is the query target (5358); it enforces limits + work block and
    # forwards allowed queries to the stub upstream.
    stub(); time.sleep(0.2)
    proc = start_app(env)
    try:
        # pages + endpoints exist with the new fields
        code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",
            "http://127.0.0.1:%d/work.html" % APP_WEB], capture_output=True, text=True).stdout
        check("/work.html served (200)", code == "200")
        body = subprocess.run(["curl","-s","http://127.0.0.1:%d/work.html" % APP_WEB], capture_output=True, text=True).stdout
        check("/work.html is the pomodoro goal page",
              "Daily goal" in body and "Start Work" in body and "set_goal" in body)
        s = http("GET", "/api/work")[1]
        check("GET /api/work has the new fields",
              all(k in s for k in ("goal_ms", "work_interval_ms", "rest_interval_ms",
                                   "accrued_ms", "rested_ms", "cycles", "phase", "log")))

        # add an absolute limit + a free time-limit
        http("POST", "/api/limits", {"name": "prom", "type": "absolute", "domains": ["prom.ua"]})
        http("POST", "/api/limits", {"name": "free", "type": "time", "domains": ["free.ua"],
            "grace_min": 40, "cooldown_min": 120})

        # baseline
        check("free.ua forwards BEFORE work (9.9.9.9)", dns_query("free.ua") == "9.9.9.9")
        check("prom.ua blocked BEFORE work (0.0.0.0)", dns_query("prom.ua") == "0.0.0.0")

        # start -> WORK stretch: block ON
        http("POST", "/api/work", {"action": "add_card", "name": "Task", "kind": "task"})
        s = http("POST", "/api/work", {"action": "set_goal", "ms": 2000})[1]
        check("set_goal over HTTP", s["goal_ms"] == 2000)
        s = http("POST", "/api/work", {"action": "set_interval", "work_ms": 1500, "rest_ms": 1000})[1]
        check("set_interval over HTTP", s["work_interval_ms"] == 1500 and s["rest_interval_ms"] == 1000)
        s = http("POST", "/api/work", {"action": "start"})[1]
        check("start arms the block window", s["session"] == "working" and s["work_until_ms"] > NOW())
        check("free.ua BLOCKED during the work stretch (0.0.0.0)", dns_query("free.ua") == "0.0.0.0")

        # the 1.5 s work stretch ends -> REST: block OFF (the pomodoro break)
        _t = 0; s = {}
        while s.get("session") != "resting" and _t < 30:
            time.sleep(0.25); _t += 0.25
            s = http("POST", "/api/work", {"action": "tick", "delta_ms": 10_000_000})[1]
        check("work stretch ends -> rest stretch over HTTP", s.get("session") == "resting")
        time.sleep(0.3)
        check("free.ua UN-BLOCKED during the rest stretch (9.9.9.9)", dns_query("free.ua") == "9.9.9.9")

        # the 1 s rest ends -> WAITING: block stays OFF, NO auto work stretch
        _t = 0
        while s.get("session") != "waiting" and _t < 30:
            time.sleep(0.25); _t += 0.25
            s = http("POST", "/api/work", {"action": "tick", "delta_ms": 10_000_000})[1]
        check("rest stretch ends -> WAITING over HTTP (no auto work)",
              s.get("session") == "waiting" and s.get("awaiting_work") is True)
        time.sleep(0.3)
        check("free.ua stays UN-BLOCKED while waiting (9.9.9.9)", dns_query("free.ua") == "9.9.9.9")
        # resume -> next WORK stretch: block ON again
        s = http("POST", "/api/work", {"action": "resume"})[1]
        check("resume from WAITING starts a work stretch",
              s.get("session") == "working" and s.get("work_until_ms") > NOW())
        time.sleep(0.2)
        check("next work stretch re-blocks free.ua (0.0.0.0)", dns_query("free.ua") == "0.0.0.0")

        # goal (2 s of work) is met inside the 2nd stretch -> success, stays free
        _t = 0
        while s.get("session") != "success" and _t < 30:
            time.sleep(0.25); _t += 0.25
            s = http("POST", "/api/work", {"action": "tick", "delta_ms": 10_000_000})[1]
        check("session reaches success over HTTP", s.get("session") == "success" and s.get("complete"))
        time.sleep(0.3)
        check("free.ua FORWARDS after success (un-blocked)", dns_query("free.ua") == "9.9.9.9")
        today = s.get("date")
        check("day stored in the work-data log (done)", any(
            e["date"] == today and e["done"] and not e["failed"] and e["worked_ms"] >= 1500
            for e in s.get("log", [])))
    finally:
        stop_app(proc)

    # ---- legacy stuck state: the old v6 "failed" self-lock heals to idle
    # (re-start the app over a crafted state whose date is in the past AND
    # whose session is the old "failed" one: the first load/status heals day +
    # session; the day is still kept in the log as failed data) ----
    with open(WORK_FILE, "w") as f:
        json.dump({"date": "1999-01-01", "session": "failed", "phase": "work",
                   "phase_end_ms": NOW() + 30000, "accrued_ms": 500.0, "rested_ms": 0.0,
                   "cycles": 0, "goal_ms": 2000, "work_ms": 1500, "rest_ms": 1000,
                   "penalty_until_ms": NOW() + 3_600_000, "penalty_ms": 2000,
                   "cards": [], "active_card_id": None, "log": []}, f)
    proc = start_app(env)
    try:
        s = http("GET", "/api/work")[1]
        check("legacy 'failed' self-lock state heals to IDLE via HTTP",
              s.get("session") == "idle" and s.get("awaiting_work") is False)
        check("worked day kept in the log as failed (stored data only)", any(
            e["date"] == "1999-01-01" and e["failed"] and not e["done"] for e in s.get("log", [])))
        time.sleep(0.3)
        check("no penalty: free.ua still FORWARDS (9.9.9.9)", dns_query("free.ua") == "9.9.9.9")
        s = http("POST", "/api/work", {"action": "start"})[1]
        check("timer starts right away after a failed day (never locked)",
              s.get("session") == "working" and s.get("work_until_ms") > NOW())
        time.sleep(0.3)
        check("work stretch blocks as usual (0.0.0.0)", dns_query("free.ua") == "0.0.0.0")
    finally:
        stop_app(proc)
        rm(WORK_DB); rm_workfile()
    return ok


if __name__ == "__main__":
    u = unit_tests()
    i = integration_tests()
    print("ALL WORK TESTS PASSED" if (u and i) else "SOME WORK TESTS FAILED")
    sys.exit(0 if (u and i) else 1)
