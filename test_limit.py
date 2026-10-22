"""Per-limit, one-directional (add-only) domain-management tests (unit + live HTTP).

This is the feature the "group" idea was refined into: clicking a limit opens a
dedicated detail page (`limit.html` at `/limit/<id>`) where you can see the
limit's type / conditions / domains and ADD new domains. Adding is deliberately
one-directional — there is NO delete/remove-domain endpoint anywhere, so undoing
an add is only possible by hand-editing the hidden DB.

Covers:
  unit (Store.add_domains): append, dedup, validation, KeyError on missing limit
  integration (live app.py): GET /api/limits/<id>, POST /domains (201/added/dedup),
      GET /limit/<id> serves limit.html, the guarantee that DELETE /domains is a
      no-op (no such endpoint), and validation (400 / 404).

Run with the gateway stopped so :5358 / :8080 are free (see README §8).
"""
import sys, os, time, subprocess, socket, json, urllib.request

BASE = "/home/pietro/limiter"
sys.path.insert(0, BASE)

LIM_DB = "/tmp/test_limit_limits.db"

def rm(p):
    for f in (p, p + "-wal", p + "-shm"):
        if os.path.exists(f):
            os.remove(f)

# ---------------------------------------------------------------------------
# unit tests (Store only)
# ---------------------------------------------------------------------------
def unit_tests():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

    rm(LIM_DB)
    import db as DB, store as ST
    store = ST.Store(LIM_DB)
    lid = store.add({"name": "prom", "type": "absolute", "domains": ["prom.ua"]})["id"]

    # append new domains, deduped
    lim, n = store.add_domains(lid, ["fb.com", "x.com"])
    check("add_domains appends + dedups", n == 2 and set(lim["domains"]) == {"prom.ua", "fb.com", "x.com"})

    # re-adding changes nothing (it can never be removed)
    lim, n = store.add_domains(lid, ["prom.ua", "y.com"])
    check("add dedups (existing ignored)", n == 1 and lim["domains"].count("prom.ua") == 1)

    # validation: empty / invalid domain set
    for bad in ([], ["not a domain"]):
        try:
            store.add_domains(lid, bad); raised = False
        except ValueError:
            raised = True
        check("add_domains rejects bad input (empty/invalid)", raised)

    # unknown limit -> KeyError
    try:
        store.add_domains(999999, ["a.com"]); raised = False
    except KeyError:
        raised = True
    check("add_domains on unknown limit -> KeyError", raised)

    rm(LIM_DB)
    return ok

# ---------------------------------------------------------------------------
# integration tests (live app.py over HTTP)
# ---------------------------------------------------------------------------
def http(method, path, body=None, base="http://127.0.0.1:8080"):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def integration_tests():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

    rm(LIM_DB)
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "app.py"),
        "--port", "5358", "--web", "8080", "--uphost", "127.0.0.1", "--upport", "5354",
        "--db", LIM_DB, "--no-doh", "--no-bypass"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                if http("GET", "/api/health")[1].get("ok"): break
            except Exception:
                pass
            time.sleep(0.2)

        # create a limit
        code, j = http("POST", "/api/limits", {"name": "prom", "type": "absolute", "domains": ["prom.ua"]})
        check("POST /api/limits -> 201", code == 201 and j["type"] == "absolute")
        lid = j["id"]

        # single-limit fetch
        code, j = http("GET", "/api/limits/%d" % lid)
        check("GET /api/limits/<id> -> 200 with domains", code == 200 and j["domains"] == ["prom.ua"])

        # detail page served (HTML; fetched via curl since http() expects JSON)
        raw = subprocess.run(["curl", "-s", "http://127.0.0.1:8080/limit/%d" % lid],
                             capture_output=True, text=True).stdout
        check("GET /limit/<id> serves limit.html", "Add domains" in raw)

        # add domains
        code, j = http("POST", "/api/limits/%d/domains" % lid, {"domains": ["fb.com", "x.com"]})
        check("POST /domains -> 201 added 2", code == 201 and j["added"] == 2 and len(j["limit"]["domains"]) == 3)

        # dedup
        code, j = http("POST", "/api/limits/%d/domains" % lid, {"domains": ["prom.ua", "y.com"]})
        check("POST /domains dedups (added 1)", code == 201 and j["added"] == 1 and len(j["limit"]["domains"]) == 4)

        # THE GUARANTEE: no delete/remove-domain endpoint. DELETE hits the shared
        # do_DELETE which only owns /api/limits/<id>, so a /domains path is a no-op.
        code, _ = http("DELETE", "/api/limits/%d/domains" % lid)
        check("no delete-domain endpoint (not 2xx)", code < 100 or code >= 400)
        code, j = http("GET", "/api/limits/%d" % lid)
        check("domains still present after the (no-op) DELETE", len(j["domains"]) == 4)

        # validation
        code, _ = http("POST", "/api/limits/%d/domains" % lid, {"domains": ["not a domain"]})
        check("POST /domains invalid -> 400", code == 400)
        code, _ = http("GET", "/api/limits/99999")
        check("GET /api/limits/<unknown> -> 404", code == 404)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        rm(LIM_DB)
    return ok


if __name__ == "__main__":
    u = unit_tests()
    i = integration_tests()
    print("ALL LIMIT TESTS PASSED" if (u and i) else "SOME LIMIT TESTS FAILED")
    sys.exit(0 if (u and i) else 1)
