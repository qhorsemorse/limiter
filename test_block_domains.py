#!/usr/bin/env python3
"""Feature: per-limit block-only domains.

A *trigger* domain (limit["domains"]) arms the grace timer when reached
(detection uses E.covers). A *block-only* domain (limit["block_domains"])
is blocked once the limit runs out (evaluate uses E.covers_block) but NEVER
arms the timer. This solves: "limit youtube with lots of domains, but those
domains also fire during web searches" — put only the ones you want to
trigger the timer in `domains`, and the noise domains in `block_domains`.

Covers:
  * engine: covers (trigger) vs covers_block (trigger ∪ block-only) vs evaluate
  * store:  add() accepts block_domains; add_block_domains() dedups, one-directional
  * db:     block_domains column round-trip + append dedup
  * e2e:    secondary syncs block_domains; trigger arms detection, block-only does
            not; once the limit is out BOTH are blocked through the real DoH wire
"""
import sys, os, time, json, ssl, socket, struct, threading, tempfile
import http.client as httplib
sys.path.insert(0, "/home/pietro/limiter")
import engine as E, db as DB, store as ST

ok = True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name); ok = ok and bool(cond)

# ---------- unit: engine ----------
def lim():
    return {"id": 1, "type": "time",
            "domains": ["youtube.com", "www.youtube.com"],
            "block_domains": ["googlevideo.com", "ytimg.com"],
            "grace_ms": 2_400_000, "cooldown_ms": 7_200_000,
            "state": "free", "grace_until": None, "cooldown_until": None}

L = lim()
check("trigger domain: covers=True", E.covers(L, "youtube.com") is True)
check("trigger subdomain: covers=True", E.covers(L, "www.youtube.com") is True)
check("block-only domain: covers=False (never arms)", E.covers(L, "googlevideo.com") is False)
check("block-only subdomain: covers=False", E.covers(L, "a.youtube.com.evil.com") is False)
check("covers_block(trigger)", E.covers_block(L, "youtube.com") is True)
check("covers_block(block-only)", E.covers_block(L, "googlevideo.com") is True)
check("covers_block(block-only subdomain)", E.covers_block(L, "vd.googlevideo.com") is True)
check("covers_block(unrelated)=False", E.covers_block(L, "example.org") is False)

L["state"] = "blocked"
check("evaluate block-only while BLOCKED -> blocked", E.evaluate([L], "googlevideo.com") == (True, 1))
check("evaluate trigger while BLOCKED -> blocked", E.evaluate([L], "youtube.com") == (True, 1))
check("evaluate unrelated -> not blocked", E.evaluate([L], "example.org")[0] is False)
L["state"] = "free"
check("evaluate block-only while FREE -> allowed", E.evaluate([L], "googlevideo.com") == (False, None))

# ---------- unit: db round-trip + append dedup (limit A) ----------
tmp = tempfile.mkdtemp()
dbp = os.path.join(tmp, "limits.db")
con = DB.init_db(dbp)
check("db: idempotent migration has block_domains",
      "block_domains" in {r[1] for r in con.execute("PRAGMA table_info(limits)")})
lid_db = DB.add_limit(con, {"name": "v", "type": "time", "domains": ["youtube.com"],
                         "block_domains": ["googlevideo.com"],
                         "grace_ms": 2_400_000, "cooldown_ms": 7_200_000})
check("db: add stores block_domains", DB.get_limit(con, lid_db)["block_domains"] == ["googlevideo.com"])
n1 = DB.append_block_domains_to_limit(con, lid_db, ["ytimg.com", "googlevideo.com"])
check("db: append dedups (added 1 of 2)", n1 == 1 and DB.get_limit(con, lid_db)["block_domains"] == ["googlevideo.com", "ytimg.com"])
n2 = DB.append_block_domains_to_limit(con, lid_db, [])
check("db: append empty -> 0", n2 == 0)
check("db: append unknown limit -> None", DB.append_block_domains_to_limit(con, 999999, ["x.com"]) is None)

# ---------- unit: store (fresh limit B) ----------
st = ST.Store(dbp)
det = st.get_limit(lid_db)
check("store.add: reads block_domains from db", det["block_domains"] == ["googlevideo.com", "ytimg.com"])
lid_s = DB.add_limit(con, {"name": "w", "type": "time", "domains": ["youtu.be"],
                         "block_domains": ["googlevideo.com"],
                         "grace_ms": 2_400_000, "cooldown_ms": 7_200_000})
st.limits = DB.list_limits(st.con)  # pick up new row
check("store.add: exposes block_domains", st.get_limit(lid_s)["block_domains"] == ["googlevideo.com"])
det, n = st.add_block_domains(lid_s, ["ytimg.com", "googlevideo.com", "not a domain"])
check("store.add_block_domains dedups+validates (added 1)",
      n == 1 and "ytimg.com" in det["block_domains"] and "googlevideo.com" in det["block_domains"])
try:
    st.add_block_domains(424242, ["x.com"]); raised = False
except KeyError:
    raised = True
check("store.add_block_domains unknown limit -> KeyError", raised)
try:
    st.add_block_domains(lid_s, ["not a domain"]); raised = False
except ValueError:
    raised = True
check("store.add_block_domains rejects bad input", raised)
# detection only arms on trigger domains (not block-only)
st.detect("googlevideo.com")
det = st.get_limit(lid_s)
check("store.detect: block-only domain does NOT arm grace", det["grace_until"] is None)
st.detect("youtu.be")
det = st.get_limit(lid_s)
check("store.detect: trigger domain DOES arm grace", det["grace_until"] is not None)

# ========================= E2E (real DoH wire) =========================
import subprocess, urllib.request, ssl, http.client as httplib, dns as DNS

def popen(port):
    s=socket.socket(); s.settimeout(0.4)
    try: s.connect(("127.0.0.1",port)); return True
    except Exception: return False
    finally: s.close()

def http_json(port, method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    hdr={"Content-Type":"application/json"} if data else {}
    req=urllib.request.Request("http://127.0.0.1:%d%s"%(port,path),data=data,method=method,headers=hdr)
    with urllib.request.urlopen(req,timeout=5) as r:
        return r.getcode(), r.read().decode()

def doh_probe(port, host):
    from doh import build_wire_query
    cert=os.path.join(BASE,"certs","ca.pem")
    wire=build_wire_query(host)
    req=urllib.request.Request("https://127.0.0.1:%d/dns-query"%port,data=wire,
        headers={"Content-Type":"application/dns-message"},method="POST")
    ctx=ssl.create_default_context(cafile=cert); ctx.check_hostname=False
    with urllib.request.urlopen(req,context=ctx,timeout=6) as r:
        resp=r.read()
    print("DBGPROBE host=%s len=%d last8=%s" % (host, len(resp), resp[-8:].hex()), flush=True)
    return "BLOCKED" if resp.endswith(b"\x00\x04\x00\x00\x00\x00") else "FORWARD"

def central_state(dbp, lid):
    c=DB.init_db(dbp); r=c.execute("SELECT state,grace_until,cooldown_until FROM limits WHERE id=?",(lid,)).fetchone(); c.close()
    return r

WEB1=9099; WEB2=9098; DOH2=9445; UPP=9451
BASE=os.path.dirname(os.path.abspath(__file__))   # dir containing this test file (=/home/pietro/limiter)
procC=procS=None; ss=None; dbp=None; lid_e2e=None

def free_port(port):
    """Kill any stray process holding `port` (from a prior crashed run) so this
    run starts clean. SIGKILL as a fallback if SIGTERM doesn't release it."""
    import os, signal as S
    inode=None
    for line in open("/proc/net/tcp").read().splitlines()[1:]:
        pr=line.split()
        if len(pr)<10 or pr[3]!="0A": continue
        try: pport=int(pr[1].rsplit(":")[1],16)
        except ValueError: continue
        if pport==port: inode=pr[9]; break
    if inode is None: return
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try: fds=os.listdir("/proc/%s/fd"%pid)
        except OSError: continue
        for fd in fds:
            try:
                if os.readlink("/proc/%s/fd/%s"%(pid,fd))=="socket:[%s]"%inode:
                    try: os.kill(int(pid), S.SIGKILL if False else S.SIGTERM)
                    except OSError: pass
                    break
            except OSError: pass

try:
    for _p in (WEB1,WEB2,DOH2,UPP): free_port(_p)
    time.sleep(0.3)
    dbp=os.path.join(tempfile.mkdtemp(),"limits.db")
    # stub upstream that answers A with 1.2.3.4
    ss=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); ss.bind(("127.0.0.1",UPP)); ss.settimeout(0.5)
    def stubloop():
        while True:
            try: d,a=ss.recvfrom(4096)
            except socket.timeout: continue
            except OSError: break
            try:
                rr=DNS.parse_query(d)
                if not rr: continue
                tid,qs=rr; an=sum(1 for _,q,_ in qs if q==1)
                resp=struct.pack(">HHHHHH",tid,0x8180,len(qs),an,0,0)
                for _,_,qr in qs: resp+=qr
                if an>0: resp+=b"\xc0\x0c"+struct.pack(">HHIH",1,1,30,4)+socket.inet_aton("1.2.3.4")
                ss.sendto(resp,a)
            except Exception: pass
    threading.Thread(target=stubloop,daemon=True).start()

    procC=subprocess.Popen([sys.executable,"app.py","--web",str(WEB1),"--dohport","0",
        "--no-sinkhole","--no-doh","--no-bypass","--db",dbp],cwd=BASE,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for _ in range(40):
        if popen(WEB1): break
        time.sleep(0.2)
    # create the time limit: trigger=[youtube.com], block-only=[googlevideo.com, ytimg.com]
    code,j=http_json(WEB1,"POST","/api/limits",{
        "name":"yt","type":"time","domains":["youtube.com"],
        "block_domains":["googlevideo.com","ytimg.com"],"grace_min":1,"cooldown_min":1})
    check("e2e: POST /api/limits accepts block_domains (201)", code==201)
    lid_e2e=json.loads(j)["id"]
    check("e2e: GET returns block_domains",
          json.loads(http_json(WEB1,"GET","/api/limits/%d"%lid_e2e)[1])["block_domains"]==["googlevideo.com","ytimg.com"])

    env=dict(os.environ, REMOTE_CACHE_DB=os.path.join(tempfile.mkdtemp(),"cache.db"), REMOTE_TTL="2")
    procS=subprocess.Popen([sys.executable,"secondary.py","--central","http://127.0.0.1:%d"%WEB1,
        "--web",str(WEB2),"--dohport",str(DOH2),"--certdir",os.path.join(BASE,"certs"),
        "--uphost","127.0.0.1","--upport",str(UPP),"--ttl","2"],
        cwd=BASE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
    for _ in range(60):
        if popen(WEB2): break
        time.sleep(0.2)
    # verify the secondary synced block_domains
    slims=json.loads(http_json(WEB2,"GET","/api/limits")[1])
    yt=[l for l in slims if l["id"]==lid_e2e][0]
    check("e2e: secondary cache synced block_domains",
          yt["block_domains"]==["googlevideo.com","ytimg.com"])

    # PHASE FREE: detection
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:%d/api/remote/status"%WEB2,timeout=5) as r:
            print("DBGSTATUS", r.read().decode(), flush=True)
    except Exception as e:
        print("DBGSTATUS ERR", repr(e), flush=True)
    check("e2e: FREE block-only domain forwards", doh_probe(DOH2,"googlevideo.com")=="FORWARD")
    time.sleep(1.1)
    st0=central_state(dbp,lid_e2e)
    check("e2e: block-only domain did NOT arm central grace", st0[1] is None)
    check("e2e: FREE trigger domain forwards", doh_probe(DOH2,"youtube.com")=="FORWARD")
    time.sleep(1.1)
    st1=central_state(dbp,lid_e2e)
    check("e2e: trigger domain DID arm central grace", st1[1] is not None)

    # PHASE BLOCKED: force the central limit blocked, reload central + secondary
    procS.terminate(); procC.terminate()
    for p in (procS,procC):
        try: p.wait(timeout=3)
        except: p.kill()
    con=DB.init_db(dbp)
    DB.save_state(con, lid_e2e, "blocked",
                  None, time.time()*1000+3_600_000)
    con.close()
    procC=subprocess.Popen([sys.executable,"app.py","--web",str(WEB1),"--dohport","0",
        "--no-sinkhole","--no-doh","--no-bypass","--db",dbp],cwd=BASE,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    procS=subprocess.Popen([sys.executable,"secondary.py","--central","http://127.0.0.1:%d"%WEB1,
        "--web",str(WEB2),"--dohport",str(DOH2),"--certdir",os.path.join(BASE,"certs"),
        "--uphost","127.0.0.1","--upport",str(UPP),"--ttl","1"],
        cwd=BASE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=dict(os.environ,REMOTE_TTL="1"))
    for _ in range(60):
        if popen(WEB2): break
        time.sleep(0.2)
    time.sleep(1.5)  # let secondary re-sync the blocked state
    check("e2e: BLOCKED trigger domain blocked", doh_probe(DOH2,"youtube.com")=="BLOCKED")
    check("e2e: BLOCKED block-only domain blocked (THE feature)", doh_probe(DOH2,"googlevideo.com")=="BLOCKED")
    check("e2e: BLOCKED unrelated domain still forwards", doh_probe(DOH2,"example.org")=="FORWARD")
finally:
    for p in (procS,procC):
        if p is None: continue
        try: p.terminate()
        except Exception: pass
    for p in (procS,procC):
        if p is None: continue
        try: p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass
    for _p in (WEB1,WEB2,DOH2,UPP): free_port(_p)   # make sure nothing leaks
    if ss is not None:
        try: ss.close()
        except Exception: pass

print("\nFULL SUITE:", "ALL PASSED" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
