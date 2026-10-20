"""Bypass-protection layer for the DNS sinkhole.

Why this exists
---------------
A pure DNS sinkhole has two well-known bypasses:

  1. DNS-over-HTTPS (DoH)  -- the client encrypts its DNS query over TLS to a
     public resolver (Cloudflare 1.1.1.1, Google 8.8.8.8, Quad9 9.9.9.9, ...).
     The sinkhole only inspects *plain* DNS, so DoH queries never reach it and
     blocked domains still resolve.

  2. VPN                   -- a VPN tunnels all traffic out to a server. Those
     tunnel packets still originate from this machine, so if we drop outbound
     traffic to the VPN peer IP, the tunnel can never establish.

Both are closed at the network layer with iptables. This module only uses the
Python stdlib (subprocess to invoke the OS firewall tooling) -- no external
deps. Applying rules needs root/sudo; generating + validating them does not.

Design (what gets installed)
----------------------------
  * nat table : redirect every locally-generated plain DNS (udp/tcp :53) to the
                sinkhole, EXCEPT packets to the sinkhole's own upstream (so the
                sinkhole's own forwards cannot loop back into itself).
  * filter tbl: DROP outbound :443 to known DoH servers (kills DoH) and DROP
                all traffic to the configured VPN peer IPs (kills the tunnel).

Rules are emitted as an `iptables-restore` ruleset and VALIDATED with
`iptables-restore --test` (root-free) before they are ever applied.
"""
import os, socket, sys, tempfile, subprocess, threading

# Public resolvers that serve DoH over TCP/443 (name -> list of IPs).
DOH_ENDPOINTS = {
    "cloudflare": ["1.1.1.1", "1.0.0.1"],
    "google":     ["8.8.8.8", "8.4.4.4"],
    "quad9":      ["9.9.9.9", "149.128.0.1"],
    "stakedns":   ["45.45.45.45", "45.45.46.46"],
    "dnslee":     ["77.88.200.70", "88.88.200.70"],
    "yandex":     ["67.34.107.184", "91.220.246.184"],
}

# Common VPN protocol ports -- informational only. VPN traffic is blocked by
# peer IP, not port, since a tunnel may use any destination port.
VPN_PORTS = [1194, 1198, 51820, 5182, 443]

_lock = threading.Lock()


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# ---------------- rule generation (pure, root-free, unit-testable) ----------------
def generate_redirect_rules(uphost, sinkhole_port):
    """Redirect all local DNS to the sinkhole, except the sinkhole's own
    upstream (uphost) to avoid a forwarding loop. Returns list of rule specs."""
    return [
        "! --dst %s -p udp --dport 53 -j REDIRECT --to-ports %d" % (uphost, sinkhole_port),
        "! --dst %s -p tcp --dport 53 -j REDIRECT --to-ports %d" % (uphost, sinkhole_port),
    ]


def generate_doh_drop_rules(doh_names=("cloudflare", "google", "quad9")):
    """DROP outbound TCP/443 to the named DoH servers."""
    rules = []
    for name in doh_names:
        for ip in DOH_ENDPOINTS.get(name, []):
            rules.append("--dst %s/32 -p tcp --dport 443 -j DROP" % ip)
    return rules


def generate_vpn_drop_rules(vpn_ips):
    """DROP all traffic to the given VPN peer IPs (kills the tunnel)."""
    return ["--dst %s/32 -j DROP" % ip for ip in vpn_ips]


def _table_block(table, rules):
    """Assemble one `*table` / built-in chains / rules / COMMIT block."""
    body = ("*%s\n" % table +
            ":INPUT ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\n:FORWARD ACCEPT [0:0]\n")
    body += "".join("-A OUTPUT %s\n" % r for r in rules) + "COMMIT\n"
    return body


def generate_rules(uphost, sinkhole_port, vpn_ips=(),
                   doh_names=("cloudflare", "google", "quad9")):
    """Return (ruleset_str, tables).

    ruleset is a valid iptables-restore document. Feed it to `--test`
    (root-free) to validate, or to `iptables-restore` (root) to apply."""
    nat = _table_block("nat", generate_redirect_rules(uphost, sinkhole_port))
    flt = _table_block("filter",
                       generate_doh_drop_rules(doh_names) +
                       generate_vpn_drop_rules(vpn_ips))
    return nat + flt, ["nat", "filter"]


def validate(ruleset):
    """Validate an iptables-restore ruleset WITHOUT applying it.

    Returns (ok, message). ok is True when parsing succeeds. A 'Permission
    denied' from the apply phase still means the rules are syntactically valid,
    so it is treated as ok here."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".ipt", delete=False)
    path = f.name
    try:
        f.write(ruleset); f.close()
        p = subprocess.run(["iptables-restore", "--test", path],
                           capture_output=True, text=True)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0:
            return True, ""
        # Non-zero only means apply needs root (rules parsed fine).
        if "Permission denied (you must be root)" in out:
            return True, out
        return False, out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------- apply / rollback (need root) ----------------
def apply(ruleset, dry_run=False):
    """Validate + apply a ruleset via iptables-restore. Returns (ok, message)."""
    ok, msg = validate(ruleset)
    if not ok:
        return False, "pre-validation failed: %s" % msg
    if dry_run:
        return True, "dry-run: would apply the following ruleset:\n" + ruleset
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".ipt", delete=False)
    path = f.name
    try:
        f.write(ruleset); f.close()
        p = subprocess.run(["iptables-restore", path],
                           capture_output=True, text=True)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def snapshot():
    """Return current firewall ruleset so it can be restored later.
    Returns (data, ok). Needs root to read."""
    p = subprocess.run(["iptables-save"], capture_output=True, text=True)
    return (p.stdout or ""), p.returncode == 0


def rollback(data):
    """Restore a firewall snapshot previously captured with snapshot().
    Returns (ok, message). Needs root."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".ipt", delete=False)
    path = f.name
    try:
        f.write(data); f.close()
        p = subprocess.run(["iptables-restore", path],
                           capture_output=True, text=True)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def detect_vpn_peers():
    """Heuristic remote peer IPs of ESTABLISHED udp/tcp connections.

    Used to auto-suggest VPN peers. Needs only read access to `ss`; peers owned
    by other users may be hidden unless running as root."""
    p = subprocess.run(["ss", "-tun"], capture_output=True, text=True)
    peers = set()
    for line in p.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or parts[1] != "ESTAB":
            continue
        peer = parts[5].rsplit("%", 1)[0].rsplit(":", 1)[0]
        if peer and not peer.startswith("127."):
            peers.add(peer)
    return sorted(peers)


# ---------------------------------------------------------------------------
# LIVE bypass detection (no root): VPN + public DoH + browser-control state
# ---------------------------------------------------------------------------
# Everything here reads /proc and /sys only — no privilege needed for the
# machine's own processes. The point is NOT to stop a bypass by firewalling
# (that needs root) but to *notice* a bypass and react: the sinkhole arms
# nuclear mode and blocks every domain until the bypass is switched off.

# Public DoH endpoints (v4) that a browser/VPN may silently use instead of us.
PUBLIC_DOH_V4 = set()
for _ips in DOH_ENDPOINTS.values():
    PUBLIC_DOH_V4.update(_ips)

# Public DoH endpoints (v6).
PUBLIC_DOH_V6 = {
    "2606:4700:4700::1111", "2606:4700:4700::1001",   # cloudflare
    "2001:4860:4860::8888", "2001:4860:4860::8844",   # google
    "2620:fe::fe", "2620:fe::9",                       # quad9
}

# Interface name prefixes that indicate a VPN/tunnel is up.
TUN_PREFIXES = ("tun", "tap", "wg", "ipsec", "vtun", "utun", "gre", "ovpn",
                "wireguard", "l2tp", "ipip", "ppp")

# Process name substrings that indicate a VPN client is running.
VPN_PROC_KEYS = ("openvpn", "wireguard", "wg", "mullvad", "protonvpn",
                 "nordvpn", "amnezia", "outline", "outline-server", "tunnell", "vpnc")


def _proc_hex_to_ip(hexaddr):
    """Parse /proc/net/{tcp,udp}{,6} addresses 'HEXIP:HEXPORT' -> (ip, port)."""
    if ":" in hexaddr:
        iph, ph = hexaddr.rsplit(":", 1)
    else:
        iph, ph = hexaddr, "0"
    host, port = None, None
    if len(iph) == 8:                                # v4, 4 byte-swapped 16-bit words
        port = int(ph, 16)
        # v4: the 8 hex chars are a u32 in HOST byte order; reversing the 4
        # bytes on little-endian yields network order
        # (0100007F -> 7f000001 = 127.0.0.1; 0100A8C0 -> 192.168.0.1).
        raw = bytes.fromhex(iph)
        if sys.byteorder == "little":
            raw = raw[::-1]
        try:
            host = socket.inet_ntop(socket.AF_INET, raw)
        except Exception:
            host = None
    elif len(iph) == 32:                             # v6, 4 byte-swapped 32-bit words
        port = int(ph, 16)
        raw = b"".join(bytes.fromhex(iph[i:i + 8])[::-1] for i in range(0, 32, 8))
        try:
            host = socket.inet_ntop(socket.AF_INET6, raw).lower()
        except Exception:
            host = None
    return host, port


def _read_proc_net(files):
    """Yield (state, lhost, lport, rhost, rport, inode) from /proc/net/{tcp,udp}{,6}."""
    for path in files:
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            p = line.split()
            if len(p) < 10:
                continue
            try:
                state = int(p[3], 16)
                lhost, lport = _proc_hex_to_ip(p[1])
                rhost, rport = _proc_hex_to_ip(p[2])
                inode = int(p[9])
            except (ValueError, IndexError):
                continue
            if lhost and rhost:
                yield state, lhost, lport, rhost, rport, inode


def scan_public_doh():
    """Return the set of public DoH endpoints this machine currently talks to.
    Looks at ESTABLISHED(01)/SYN-SENT(02) TCP and any UDP sockets to the
    known DoH IPs on port 443 (v4+v6)."""
    files = ["/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6"]
    found = set()
    for state, lhost, lport, rhost, rport, _inode in _read_proc_net(files):
        if rport != 443:
            continue
        if rhost in PUBLIC_DOH_V4 or (rhost and rhost.lower() in PUBLIC_DOH_V6):
            found.add((rhost if rhost[0] != "::" else "::ffff:" + rhost, 443))
    return sorted("%s:%d" % (h, p) for h, p in found), (PUBLIC_DOH_V4 | PUBLIC_DOH_V6)


def parse_proc_net_route(text):
    """Pure: parse /proc/net/route text -> (gw_ip_or_None, iface_name_or_None) of the
    FIRST default route (dest 00000000)."""
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) < 4:
            continue
        if p[1] == "00000000":
            try:
                # /proc/net/route prints the gateway as a hex u32 in host byte
                # order; reverse on little-endian to get network order
                # (0100A8C0 -> 192.168.0.1).
                raw = bytes.fromhex(p[2])
                if sys.byteorder == "little":
                    raw = raw[::-1]
                gw = socket.inet_ntop(socket.AF_INET, raw)
            except Exception:
                gw = None
            return gw, p[0]
    return None, None


def vpn_status(routes_text=None, netdevs=None, proc_names=None):
    """Detect an active VPN: tun-like iface up, VPN client process, or both.
    Returns (active_bool, why_string_or_None)."""
    if routes_text is None:
        try:
            routes_text = open("/proc/net/route").read()
        except OSError:
            routes_text = ""
    if netdevs is None:
        try:
            netdevs = [d for d in os.listdir("/sys/class/net") if d != "lo"]
        except OSError:
            netdevs = []
    tun = [d for d in netdevs if d.startswith(TUN_PREFIXES)]

    names = proc_names
    if names is None:
        names = set()
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    names.add(os.readlink("/proc/%s/comm" % pid).lower())
                except OSError:
                    pass
        except OSError:
            pass
    proc_hit = None
    for nm in sorted(names or ()):
        if any(k in nm for k in VPN_PROC_KEYS):
            proc_hit = nm
            break

    why = []
    if tun:
        why.append("tunnel interface: " + ",".join(sorted(tun)))
    if proc_hit:
        why.append("vpn process: " + proc_hit)
    return (bool(why)), ("; ".join(why) if why else None)


def firefox_pids():
    """PIDs of running firefox processes (snap or deb)."""
    pids = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cl = open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if "firefox" in cl.lower():
                pids.append(int(pid))
    except OSError:
        pass
    return sorted(pids)


def _pid_sockets(pids):
    """Set of socket inodes held by the given pids (own-user processes only)."""
    inodes = set()
    for pid in pids:
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    t = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                    if t.startswith("socket:["):
                        inodes.add(int(t[8:-1]))
                except OSError:
                    pass
        except OSError:
            pass
    return inodes


def firefox_using_gateway(doh_port):
    """True if any firefox process holds a socket to 127.0.0.1:<doh_port>
    (i.e. its DNS is actually flowing through the gateway's DoH endpoint).
    Best-effort: other users' sockets are invisible without root."""
    pids = firefox_pids()
    if not pids:
        return False, 0
    inodes = _pid_sockets(pids)
    if not inodes:
        return False, 0
    hits = 0
    for state, lhost, lport, rhost, rport, inode in _read_proc_net(
            ["/proc/net/tcp", "/proc/net/tcp6"]):
        if inode not in inodes:
            continue
        if state not in (0x01, 0x08, 0x06):          # ESTABLISHED, CLOSE_WAIT, LAST_ACK
            continue
        pair = ((lhost, lport), (rhost, rport))
        if ("127.0.0.1", doh_port) in pair:
            hits += 1
    return hits > 0, hits


def firefox_off_gateway_dns():
    """Positive evidence that Firefox is resolving OUTSIDE our gateway:
    Firefox-held sockets to :53 (plain OS resolver, e.g. 127.0.0.53) or to
    known public DoH endpoints on :443.

    This is what 'Firefox stopped using our DoH' actually looks like from the
    outside. An idle browser has NO sockets at all, so absence of a
    127.0.0.1:<doh_port> socket is NOT evidence of a bypass.
    Returns (active_bool, ["ip:port", ...])."""
    pids = firefox_pids()
    if not pids:
        return False, []
    inodes = _pid_sockets(pids)
    if not inodes:
        return False, []
    peers = set()
    files = ["/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6"]
    for _state, _lhost, _lport, rhost, rport, inode in _read_proc_net(files):
        if inode not in inodes or not rhost:
            continue
        if rport == 53:
            peers.add("%s:53" % rhost)
        elif rport == 443 and (rhost in PUBLIC_DOH_V4 or rhost.lower() in PUBLIC_DOH_V6):
            peers.add("%s:443" % rhost)
    return bool(peers), sorted(peers)


def detect(lan_gateway="192.168.0.1", doh_port=8443):
    """Top-level bypass probe. Pure IO, root-free. Returns a JSON-able dict:
    {
      "vpn":        {"active": bool, "why": str|None},
      "public_doh": {"active": bool, "peers": ["ip:443", ...]},
      "firefox":    {"running": bool, "pids": [...], "using_gateway": bool},
      "gateway_changed": bool,
    }"""
    routes_text = ""
    try:
        routes_text = open("/proc/net/route").read()
    except OSError:
        pass
    gw, _dev = parse_proc_net_route(routes_text)
    vpn_active, vpn_why = vpn_status(routes_text)
    peers, _known = scan_public_doh()
    ff = firefox_pids()
    using, _n = firefox_using_gateway(doh_port)
    off_active, off_peers = firefox_off_gateway_dns()
    gw_changed = bool(lan_gateway and gw and gw != lan_gateway)
    return {
        "vpn": {"active": bool(vpn_active or gw_changed),
                "why": vpn_why or ("default gateway changed to %s (expected %s)" % (gw, lan_gateway) if gw_changed else None)},
        "gateway": gw,
        "public_doh": {"active": bool(peers), "peers": peers},
        "firefox": {"running": bool(ff), "pids": ff, "using_gateway": using},
        "off_dns": {"active": off_active, "peers": off_peers},
        "gateway_changed": gw_changed,
    }
