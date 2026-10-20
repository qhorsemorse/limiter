import sys, os, time, socket, struct, subprocess, shutil, json, urllib.request
import dns as DNS

BASE="/home/pietro/limiter"
DB="/tmp/test_app_limits.db"
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f): os.remove(f)

def rm_db():
    for f in (DB, DB + "-wal", DB + "-shm"):
        if os.path.exists(f): os.remove(f)

def http(method, path, body=None):
    url="http://127.0.0.1:8080"+path
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type":"application/json"} if data else {})
    try:
        r=urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def build_query(name, tid=0x7799):
    q=struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    lab=b""
    for p in name.split("."): lab += bytes([len(p)])+p.encode()
    return q+lab+b"\x00"+struct.pack(">HH",1,1)

def dns_query(name):
    c=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.bind(("127.0.0.1",0)); c.settimeout(3)
    c.sendto(build_query(name), ("127.0.0.1", 5353))
    resp,_=c.recvfrom(4096); c.close()
    tid,flags,qd,an,ns,ar=struct.unpack(">HHHHHH", resp[:12])
    off=12
    # skip question section (handles compression pointers)
    for _ in range(qd):
        while True:
            L=resp[off]
            if L==0: off+=1; break
            if L&0xc0: off+=2; break
            off+=1+L
        off+=4
    # parse answers
    for _ in range(an):
        if resp[off]==0xc0: off+=2
        else:
            while True:
                L=resp[off]
                if L==0: off+=1; break
                if L&0xc0: off+=2; break
                off+=1+L
        typ,cls,ttl,rdlen=struct.unpack(">HHIH", resp[off:off+10]); off+=10
        ip=resp[off:off+rdlen]; off+=rdlen
        if typ==1 and rdlen==4:
            return ".".join(str(b) for b in ip)
    return None

# stub upstream answering A->9.9.9.9, reusing real DNS parse/echo logic
def stub():
    s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1",5354))
    def loop():
        while True:
            try: d,a=s.recvfrom(4096)
            except socket.timeout: continue
            except OSError: break
            parsed=DNS.parse_query(d)
            if not parsed: continue
            tid,questions=parsed
            an=sum(1 for _,qt,_ in questions if qt==1)
            resp=struct.pack(">HHHHHH",tid,0x8180,len(questions),an,0,0)
            for name,qtype,qraw in questions:
                resp+=qraw
            if an > 0:
                resp+=b"\xc0\x0c"+struct.pack(">HHIH",1,1,30,4)+socket.inet_aton("9.9.9.9")
            try: s.sendto(resp,a)
            except OSError: break
    threading.Thread(target=loop,daemon=True).start()
    return s
import threading
stub()
time.sleep(0.2)

proc=subprocess.Popen([sys.executable, os.path.join(BASE,"app.py"),
    "--port","5353","--web","8080","--uphost","127.0.0.1","--upport","5354","--db",DB],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
# wait for boot
for _ in range(50):
    try:
        http("GET","/api/health"); break
    except Exception: time.sleep(0.2)

ok=True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name); ok = ok and cond

try:
    check("GET /api/limits empty", http("GET","/api/limits")[1]==[])
    st,j=http("POST","/api/limits",{"name":"prom","type":"absolute","domains":["prom.ua"]})
    check("POST absolute -> 201", st==201 and j["type"]=="absolute")
    st,j=http("GET","/api/limits")
    check("GET shows 1 limit", len(j)==1 and j[0]["domains"]==["prom.ua"])
    check("prom.ua blocked via DNS -> 0.0.0.0", dns_query("prom.ua")=="0.0.0.0")

    st,j=http("POST","/api/limits",{"name":"olx","type":"time","domains":["olx.ua"],"grace_min":40,"cooldown_min":120})
    check("POST time -> 201", st==201 and j["type"]=="time")
    st,j=http("GET","/api/limits")
    check("GET shows 2 limits", len(j)==2)
    check("olx.ua (free) forwards -> 9.9.9.9", dns_query("olx.ua")=="9.9.9.9")
    _,j=http("GET","/api/limits")  # re-fetch: observe armed grace after access
    o=[l for l in j if l["name"]=="olx"][0]
    check("olx armed grace on access", o["grace_until"] is not None)

    # validation
    st,j=http("POST","/api/limits",{"type":"time","domains":["ok.ua"]})
    check("POST time missing mins -> 400", st==400)
    st,j=http("POST","/api/limits",{"type":"bogus","domains":["x.com"]})
    check("POST bad type -> 400", st==400)

    # delete (fetch fresh list; last `j` above was an error response)
    _,j=http("GET","/api/limits")
    lid=[l["id"] for l in j if l["name"]=="olx"][0]
    st,j=http("DELETE","/api/limits/%d"%lid)
    check("DELETE olx -> deleted true", st==200 and j["deleted"]==True)
    st,j=http("GET","/api/limits")
    check("GET back to 1 limit", len(j)==1)
finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()
    rm_db()

print("ALL APP TESTS PASSED" if ok else "SOME TESTS FAILED")
