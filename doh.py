"""DNS-over-HTTPS (DoH) responder — the browser front-door.

Why this exists
---------------
Firefox 157 removed the old `network.proxy.dns` preference, so there is no
way to point the browser's *plain* DNS at a custom server via prefs. What
FF157 *does* honour is its TRR/DoH settings:

    network.trr.mode = 2                      (TRRONLY: 100% of DNS via DoH)
    network.trr.uri  = https://127.0.0.1:8443/dns-query

We set both in a `user.js` inside the Firefox profile (re-applied on every
startup). The consequence: *every* DNS query the browser makes arrives here,
encrypted to us on loopback, and this module answers it with the exact same
sinkhole logic as the plain-DNS path (`dns.resolve_bytes`):

    blocked domain   -> 0.0.0.0
    allowed domain   -> forwarded to the real upstream resolver (plain UDP)

DoH is therefore the enforcement channel itself, not a bypass. If a user
switches Firefox back to plain DNS or a public DoH provider, the bypass
watcher in app.py detects it and arms nuclear mode (block everything).

The endpoint is TLS with a locally generated self-signed CA; the CA is added
to the Firefox profile's cert9.db by tools/setup_firefox.sh (no root needed).
"""
import base64, json, os, socket, ssl, struct, threading
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import dns as D
import engine as E

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CERTDIR = os.path.join(BASE, "certs")


# ---------------------------------------------------------------------------
# small wire helpers
# ---------------------------------------------------------------------------
def build_wire_query(name, qtype=1):
    """Minimal A (or given type) query for `name`, random-ish id."""
    tid = (os.getpid() ^ (len(name) * 7919)) & 0xFFFF
    lab = b""
    for part in name.split("."):
        lab += bytes([len(part)]) + part.encode("ascii", "ignore")
    return struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + lab + b"\x00" + struct.pack(">HH", qtype, 1)


def decode_name(data, off):
    """Decode a (possibly compressed) domain name at offset. Returns (name, new_off)."""
    labels = []
    jumps = 0
    while True:
        if off >= len(data):
            break
        l = data[off]
        if l == 0:
            off += 1
            break
        if l & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                break
            ptr = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            off += 2
            if jumps > 8:
                break
            sub, _ = decode_name(data, ptr)
            labels.append(sub)
            jumps += 1
            break
        labels.append(data[off + 1:off + 1 + l].decode("latin-1", "ignore"))
        off += 1 + l
    return ".".join(labels), off


def extract_answers(resp):
    """Return list of (name, qtype, ttl, rdata_bytes) from a DNS response."""
    out = []
    if len(resp) < 12:
        return out
    qd, an = struct.unpack(">HH", resp[4:8])
    off = 12
    for _ in range(qd):
        _, off = decode_name(resp, off)
        off += 4
    for _ in range(an):
        name, off = decode_name(resp, off)
        if off + 10 > len(resp):
            break
        qtype, qclass, ttl, rdlen = struct.unpack(">HHIH", resp[off:off + 10])
        off += 10
        rdata = resp[off:off + rdlen]
        off += rdlen
        out.append((name, qtype, ttl, rdata))
    return out


def _a_to_str(qtype, rdata):
    if qtype == 1 and len(rdata) == 4:
        return socket.inet_ntoa(rdata)
    if qtype == 28 and len(rdata) == 16:
        return socket.inet_ntop(socket.AF_INET6, rdata)
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _DoHHandler(BaseHTTPRequestHandler):
    server_version = "ControlLimits-DoH/1.0"

    def log_message(self, *a, **k):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code, None if code == 408 else "OK")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def _cors(self):
        if self.command == "OPTIONS":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _process(self):
        srv = self.server
        path = urlparse(self.path).path
        if path not in ("/dns-query", "/"):
            self._send(404, b"not found", "text/plain")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""

        if self.command == "GET" and not body:
            qs = parse_qs(urlparse(self.path).query)
            if "dns" in qs:                                   # RFC 8484 base64
                try:
                    raw_d = qs["dns"][0]
                    # Firefox sends RFC8484 base64url WITHOUT padding
                    body = base64.urlsafe_b64decode(raw_d + "=" * (-len(raw_d) % 4))
                except Exception:
                    self._send(400, b"bad dns= param", "text/plain")
                    return

        store, upstream = srv.limits_store, srv.upstream

        if ctype == "application/dns-json" or (body[:1] in (b"{",) and ctype != "application/dns-message"):
            self._process_json(body, store, upstream)
        else:
            self._process_wire(body, store, upstream)

    def _process_wire(self, body, store, upstream):
        if not body:
            self._send(400, b"empty dns message", "text/plain")
            return
        resp, blocked = D.resolve_bytes(store, body, upstream, channel="doh")
        if resp is None:
            self._send(400, b"unparseable dns message", "text/plain")
            return
        self._send(200, resp, "application/dns-message")

    def _process_json(self, body, store, upstream):
        try:
            req = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            self._send(400, b"bad json", "text/plain")
            return
        qname = str(req.get("Name", "")).strip().rstrip(".").lower()
        qtype = int(req.get("Type", 1) or 1)
        if not qname or "." not in qname:
            self._send(400, b"missing name", "text/plain")
            return
        wire = build_wire_query(qname, qtype)
        resp, blocked = D.resolve_bytes(store, wire, upstream, channel="doh")
        out = {"Status": 0, "TC": False, "RD": False, "RA": True, "CD": False, "AD": False,
               "Question": [{"name": qname, "type": qtype}], "Answer": []}
        if resp is None:
            out["Status"] = 1                                # FORMERR
        else:
            for name, tt, ttl, rdata in extract_answers(resp):
                ip = _a_to_str(tt, rdata)
                if tt in (1, 28):
                    out["Answer"].append({"name": name, "type": tt, "TTL": ttl,
                                          "data": _a_to_str(tt, rdata) or ""})
        self._send(200, json.dumps(out).encode(), "application/dns-json")

    def do_GET(self):
        if self._cors():
            return
        try:
            self._process()
        except Exception:
            pass

    def do_POST(self):
        if self._cors():
            return
        try:
            self._process()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------
def cert_paths(certdir=DEFAULT_CERTDIR):
    return os.path.join(certdir, "server.pem"), os.path.join(certdir, "server.key")


def tls_available(certdir=DEFAULT_CERTDIR):
    cert, key = cert_paths(certdir)
    return os.path.exists(cert) and os.path.exists(key)


def serve(store, port=8443, upstream=("127.0.0.53", 53), certdir=DEFAULT_CERTDIR, host="127.0.0.1"):
    """Start the TLS DoH responder. Returns the ThreadingHTTPServer (daemon thread)."""
    cert, key = cert_paths(certdir)
    if not (os.path.exists(cert) and os.path.exists(key)):
        raise RuntimeError("no TLS certs in %s — run tools/gen_certs.sh first" % certdir)
    httpd = ThreadingHTTPServer((host, port), _DoHHandler)
    httpd.limits_store = store
    httpd.upstream = upstream
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="doh")
    t.start()
    return httpd
