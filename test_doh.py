"""Unit tests for doh.py: TLS DoH responder answering wire (POST + RFC8484 GET)
and dns-json with the same sinkhole logic as plain DNS.

Needs the local certs (tools/gen_certs.sh); generates them if missing.
"""
import sys, os, time, socket, struct, threading, base64, json, subprocess, ssl
import http.client
sys.path.insert(0, os.path.dirname(__file__))
import store as S, doh

BASE = os.path.dirname(os.path.abspath(__file__))
CERTDIR = os.path.join(BASE, "certs")
if not doh.tls_available(CERTDIR):
    subprocess.run(["bash", os.path.join(BASE, "tools", "gen_certs.sh")], check=True)

DB = "/tmp/test_doh_limits.db"
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

st = S.Store(DB)
st.add({"name": "abs", "type": "absolute", "domains": ["bad.example"]})


def freeport():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --- stub upstream: answers every A query with 9.9.9.9 ---
def stub(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))

    def loop():
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                break
            tid = struct.unpack(">H", data[0:2])[0]
            off = 12
            while off < len(data):
                L = data[off]
                if L == 0:
                    off += 1
                    break
                off += 1 + L
            off += 4
            qsec = data[12:off]
            resp = struct.pack(">HHHHHH", tid, 0x8580, 1, 1, 0, 0) + qsec + \
                   b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 30, 4) + socket.inet_aton("9.9.9.9")
            try:
                s.sendto(resp, addr)
            except OSError:
                break

    threading.Thread(target=loop, daemon=True).start()
    return s


stub_s = stub(freeport())
stub_port = stub_s.getsockname()[1]
httpd = doh.serve(st, port=freeport(), upstream=("127.0.0.1", stub_port), certdir=CERTDIR)
doh_port = httpd.server_address[1]
time.sleep(0.3)

CA = os.path.join(CERTDIR, "ca.pem")
CTX = ssl.create_default_context(cafile=CA)


def a_ips(name, method="POST"):
    body = doh.build_wire_query(name)
    c = http.client.HTTPSConnection("127.0.0.1", doh_port, context=CTX, timeout=5)
    if method == "POST":
        c.request("POST", "/dns-query", body=body,
                  headers={"Content-Type": "application/dns-message"})
    else:
        b64 = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        c.request("GET", "/dns-query?dns=" + b64)          # unpadded, like Firefox
    r = c.getresponse()
    assert r.status == 200, (r.status, r.read()[:200])
    out = doh.extract_answers(r.read())
    c.close()
    return [socket.inet_ntop(socket.AF_INET, rd) for n, t, ttl, rd in out
            if t == 1 and len(rd) == 4]


# 1) blocked domain -> 0.0.0.0 over wire (POST)
assert a_ips("bad.example") == ["0.0.0.0"]
print("POST wire : blocked bad.example  -> 0.0.0.0  OK")

# 2) allowed domain -> forwarded via stub -> 9.9.9.9
assert a_ips("good.example") == ["9.9.9.9"]
print("POST wire : allowed good.example -> 9.9.9.9  OK")

# 3) RFC 8484 GET, base64url without padding
assert a_ips("good.example", method="GET") == ["9.9.9.9"]
print("GET  wire : base64url (RFC8484)  -> 9.9.9.9  OK")

# 4) dns-json
c = http.client.HTTPSConnection("127.0.0.1", doh_port, context=CTX, timeout=5)
c.request("POST", "/dns-query", body=json.dumps({"Name": "bad.example."}).encode(),
          headers={"Content-Type": "application/dns-json"})
r = c.getresponse()
assert r.status == 200
body = json.loads(r.read())
assert body["Status"] == 0
assert any(a.get("data") == "0.0.0.0" for a in body["Answer"]), body
c.close()
print("POST json : blocked bad.example  -> 0.0.0.0  OK")

# 5) nuclear: everything -> 0.0.0.0 EXCEPT localhost (NUCLEAR_EXEMPT)
st.set_nuclear(True, "test")
try:
    assert a_ips("good.example") == ["0.0.0.0"]
    assert a_ips("localhost") == ["9.9.9.9"]
    print("nuclear   : block all, localhost exempted  OK")
finally:
    st.set_nuclear(False, None)

# 6) stats move
before = list(st.stats["doh"])
a_ips("bad.example")        # blocked
a_ips("good.example")       # allowed
a_ips("good.example")       # allowed
after = list(st.stats["doh"])
assert after[0] - before[0] == 3, (before, after)
assert after[1] - before[1] == 1, (before, after)
print("stats     : doh total +3, blocked +1  OK")

httpd.shutdown()
httpd.server_close()
stub_s.close()
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)
print("ALL DOH TESTS PASSED")
