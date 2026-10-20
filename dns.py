"""DNS sinkhole (UDP + TCP). Blocked domains -> 0.0.0.0,
allowed queries are forwarded to an upstream resolver.

The plain-DNS server is the legacy/auxiliary front door (used by unit tests,
and — once the iptables REDIRECT rules are applied with root — by the whole
OS). The main browser path is the DoH responder in doh.py, which shares the
same resolution core: `resolve_bytes`.

Usage:
    sock, stop = serve(store, port=5354, upstream=("127.0.0.53", 53))
"""
import socket, struct, threading
import engine as E


def parse_query(data):
    if len(data) < 12:
        return None
    tid = struct.unpack(">H", data[0:2])[0]
    qd = struct.unpack(">H", data[4:6])[0]
    off, questions = 12, []
    for _ in range(min(qd, 16)):
        qstart = off
        labels = []
        while True:
            if off >= len(data):
                return None
            length = data[off]
            if length == 0:
                off += 1
                break
            if length & 0xC0 == 0xC0:     # compression pointer (rare in queries)
                off += 2
                break
            labels.append(data[off + 1:off + 1 + length].decode("latin-1", "ignore"))
            off += 1 + length
        qtype = struct.unpack(">H", data[off:off + 2])[0]
        off += 4                            # qtype + qclass
        qraw = data[qstart:off].copy() if hasattr(data, "copy") else bytes(data[qstart:off])
        questions.append((".".join(labels), qtype, qraw))
    return tid, questions


def _resp_header(tid, questions, an):
    return struct.pack(">HHHHHH", tid, 0x8180, len(questions), an, 0, 0)


def _echo_question(qraw):
    return qraw


def build_blocked(tid, questions):
    """Echo questions, answer A -> 0.0.0.0, others -> NODATA."""
    has_a = any(q[1] == 1 for q in questions)
    an = 1 if has_a else 0
    resp = _resp_header(tid, questions, an)
    for _, _, qraw in questions:
        resp += _echo_question(qraw)
    if has_a:
        resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("0.0.0.0")
    return resp


def build_nodata(tid, questions):
    resp = _resp_header(tid, questions, 0)
    for _, _, qraw in questions:
        resp += _echo_question(qraw)
    return resp


def forward(upstream, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.sendto(data, upstream)
        resp, _ = s.recvfrom(4096)
        return resp
    except Exception:
        return None
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Shared resolution core (used by the UDP/TCP sinkhole AND the DoH responder)
# ---------------------------------------------------------------------------
def resolve_bytes(store, data, upstream, channel="udp", now=None):
    """Run one DNS query through the limit engine.

    Returns (response_bytes_or_None, blocked_bool).
    blocked=True  -> answer is the sinkhole (0.0.0.0)
    blocked=False -> answer came from the real upstream resolver
    None          -> query could not be parsed
    """
    parsed = parse_query(data)
    if not parsed:
        return None, False
    tid, questions = parsed
    now = now if now is not None else E.now_ms()
    store.tick(now)

    # --- workout reward window: ALL blocking suspended (limits AND nuclear),
    #     every query goes to the real upstream
    if store.reward_active(now):
        resp = forward(upstream, data)
        if resp:
            store.count(channel, False)
            return resp, False
        # upstream unreachable: fall through to normal handling below

    # --- nuclear (bypass) mode: block EVERYTHING except the exempt local names
    if store.is_nuclear():
        for name, qtype, _ in questions:
            host = E.normalize_domain(name)
            if host and host not in E.NUCLEAR_EXEMPT:
                store.count(channel, True)
                return build_blocked(tid, questions), True

    blocked_id = None
    for name, qtype, qraw in questions:
        host = E.normalize_domain(name)
        if not host:
            continue
        blocked, lid = store.evaluate(host)
        if blocked:
            blocked_id = lid
            break
        if qtype == 1:                     # only "detect" real (A) lookups
            store.detect(host, now)

    if blocked_id is None:
        resp = forward(upstream, data)
        if resp:
            store.count(channel, False)
            return resp, False
    store.count(channel, True)
    return build_blocked(tid, questions), True


def handle_udp(sock, data, addr, store, upstream, now=None):
    """Backwards-compatible UDP entrypoint (tests, legacy path)."""
    resp, blocked = resolve_bytes(store, data, upstream, channel="udp", now=now)
    if resp is None:
        return
    sock.sendto(resp, addr)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class _Listener:
    """Holds the UDP+TCP sockets; .close() shuts all of them down.
    Kept object-like so legacy callers (tests) can do `sock.close()`."""
    def __init__(self):
        self.socks = []

    def add(self, s):
        self.socks.append(s)

    def close(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass


def serve(store, port=5354, upstream=("127.0.0.53", 53)):
    """Start UDP + TCP listeners on `port`. Returns (listener, threads).
    A bind failure is logged, not fatal (e.g. plain-DNS 5354 might be taken by
    a stale root process while the DoH front-door still works)."""
    listener = _Listener()
    threads = []

    # --- UDP
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))

        def udp_loop():
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    handle_udp(sock, data, addr, store, upstream)
                except Exception:
                    pass

        listener.add(sock)
        t = threading.Thread(target=udp_loop, daemon=True)
        t.start()
        threads.append(t)
    except OSError as e:
        print("[!] plain-DNS: cannot bind udp :%d (%s) — DoH front-door still works" % (port, e))

    # --- TCP (stub resolvers and long queries)
    try:
        tsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tsock.bind(("0.0.0.0", port))
        tsock.listen(16)
        tsock.settimeout(1.0)

        def tcp_loop():
            while True:
                try:
                    conn, addr = tsock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threads.append(threading.Thread(target=_tcp_client, args=(conn, store, upstream), daemon=True))
                threads[-1].start()

        listener.add(tsock)
        t = threading.Thread(target=tcp_loop, daemon=True)
        t.start()
        threads.append(t)
    except OSError as e:
        print("[!] plain-DNS: cannot bind tcp :%d (%s)" % (port, e))

    return listener, threads


def _tcp_client(conn, store, upstream):
    try:
        conn.settimeout(4)
        hdr = conn.recv(2)
        if len(hdr) != 2:
            return
        (ln,) = struct.unpack(">H", hdr)
        data = b""
        while len(data) < ln:
            chunk = conn.recv(ln - len(data))
            if not chunk:
                return
            data += chunk
        resp, _ = resolve_bytes(store, data, upstream, channel="udp")
        if resp:
            conn.sendall(struct.pack(">H", len(resp)) + resp)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
