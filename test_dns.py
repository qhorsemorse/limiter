import sys, os, time, socket, struct, threading, shutil
sys.path.insert(0, os.path.dirname(__file__))
import store as S, dns as D, engine as E

DB = "/tmp/test_limits.db"
if os.path.exists(DB): os.remove(DB)

st = S.Store(DB)
st.add({"name":"prom","type":"absolute","domains":["prom.ua"]})
st.add({"name":"olx","type":"time","domains":["olx.ua"],"grace_min":40,"cooldown_min":120})
st.add({"name":"free","type":"time","domains":["free.ua"],"grace_min":40,"cooldown_min":120})

# Force olx.ua into BLOCKED (simulate: accessed, grace elapsed) using REAL epoch
now = E.now_ms()
st.detect("olx.ua", now)
st.tick(now + 41*60_000)
assert st.current(st.limits[1]["id"])["state"] == "blocked", st.current(st.limits[1]["id"])

# --- stub upstream DNS that answers A -> 1.2.3.4 for everything ---
def stub():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 5354))
    def loop():
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            tid = struct.unpack(">H", data[0:2])[0]
            off = 12
            while True:
                if off >= len(data):
                    break
                L = data[off]
                if L == 0:
                    off += 1
                    break
                if L & 0xC0 == 0xC0:
                    off += 2
                    break
                off += 1 + L
            qtype = struct.unpack(">H", data[off:off+2])[0]; off += 4
            qsection = data[12:off]                       # labels + null + qtype + qclass
            resp = struct.pack(">HHHHHH", tid, 0x8010, 1, 1, 0, 0) + qsection + \
                   b"\xc0\x0c" + struct.pack(">HHIH", qtype, 1, 30, 4) + socket.inet_aton("1.2.3.4")
            try:
                s.sendto(resp, addr)
            except OSError:
                break
    threading.Thread(target=loop, daemon=True).start()
    return s

stub_s = stub()
time.sleep(0.1)
sock, th = D.serve(st, port=5353, upstream=("127.0.0.1", 5354))
time.sleep(0.2)

def query(name):
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.bind(("127.0.0.1", 0))
    q = struct.pack(">HHHHHH", 0x539a, 0x0100, 1, 0, 0, 0)
    label = b""
    for part in name.split("."):
        label += bytes([len(part)]) + part.encode()
    q += label + b"\x00" + struct.pack(">HH", 1, 1)
    c.sendto(q, ("127.0.0.1", 5353)); c.settimeout(3)
    resp, _ = c.recvfrom(4096)
    c.close()
    return resp.hex()

r_prom = query("prom.ua")
assert "00000000" in r_prom, "prom.ua (absolute) should be 0.0.0.0"
assert "01020304" not in r_prom, "prom should not forward"
print("prom.ua (absolute block)        -> 0.0.0.0  OK")

r_olx = query("olx.ua")
assert "00000000" in r_olx, "olx.ua (blocked cycle) should be 0.0.0.0"
print("olx.ua (blocked cycle)          -> 0.0.0.0  OK")

r_free = query("free.ua")
assert "01020304" in r_free, "free.ua (free cycle) should forward to 1.2.3.4"
print("free.ua (free cycle, forward)   -> 1.2.3.4  OK")
# after querying free.ua it should have armed its grace timer
free_id = [l['id'] for l in st.limits if l['name']=='free'][0]
fl = st.current(free_id)
assert fl is not None and fl["grace_until"] is not None, "free.ua should have armed grace on lookup"
print("free.ua detection armed grace   -> OK")

# not-limited domain forwards too
r_ex = query("example.com")
assert "01020304" in r_ex, "example.com should forward"
print("example.com (no limit, forward) -> 1.2.3.4  OK")

stub_s.close(); sock.close()

# --- Unit test for bug #1: multi-question blocked response strictly RFC-compliant ---
def make_qraw(name, qtype):
    lab = b""
    for part in name.split("."):
        lab += bytes([len(part)]) + part.encode()
    return lab + b"\x00" + struct.pack(">HH", qtype, 1)

q1_raw = make_qraw("blocked.example", 1)   # A
q2_raw = make_qraw("blocked.example", 28)  # AAAA
q3_raw = make_qraw("blocked.example", 65)  # HTTPS
questions_3 = [
    ("blocked.example", 1, q1_raw),
    ("blocked.example", 28, q2_raw),
    ("blocked.example", 65, q3_raw),
]
resp_3 = D.build_blocked(0x1234, questions_3)
tid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", resp_3[:12])
assert tid == 0x1234, "tid match"
assert flags == 0x8180, f"flags expected 0x8180, got {hex(flags)}"
assert qd == 3, f"qd expected 3, got {qd}"
assert an == 1, f"an expected 1, got {an}"
# Question section must be contiguous:
expected_qd = q1_raw + q2_raw + q3_raw
actual_qd = resp_3[12:12 + len(expected_qd)]
assert actual_qd == expected_qd, "question section must be contiguous without interleaved answers"
# Answer section starts immediately after question section
ans_section = resp_3[12 + len(expected_qd):]
ptr, atype, aclass, attl, ardlen = struct.unpack(">HHHIH", ans_section[:12])
assert ptr == 0xc00c, f"pointer expected 0xc00c (offset 12), got {hex(ptr)}"
assert atype == 1, "answer type A"
assert aclass == 1, "answer class IN"
assert ardlen == 4, "rdlen 4"
assert ans_section[12:16] == socket.inet_aton("0.0.0.0"), "answer IP 0.0.0.0"
assert len(ans_section) == 16, f"expected exactly 1 answer record, got len {len(ans_section)}"
print("multi-question (A+AAAA+HTTPS) blocked -> RFC-compliant 0.0.0.0  OK")

# Single AAAA query blocked -> returns valid NODATA (qd=1, an=0)
questions_aaaa = [("blocked.example", 28, q2_raw)]
resp_aaaa = D.build_blocked(0x5678, questions_aaaa)
tid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", resp_aaaa[:12])
assert qd == 1 and an == 0, f"expected qd=1 an=0, got qd={qd} an={an}"
assert resp_aaaa[12:12 + len(q2_raw)] == q2_raw, "AAAA question preserved"
assert len(resp_aaaa) == 12 + len(q2_raw), "no answers for AAAA blocked (NODATA)"
print("AAAA-only blocked -> RFC-compliant NODATA (an=0)  OK")

for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

print("ALL DNS TESTS PASSED")
