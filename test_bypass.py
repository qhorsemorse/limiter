"""Unit tests for bypass.py (the DoH/VPN bypass-protection layer).

These tests are root-free: `validate()` runs `iptables-restore --test` which
only parses the ruleset; a 'Permission denied' (apply-time) is treated as a
successful parse. The semantic assertions verify the rules are what we intend.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import bypass as B

def contains(ruleset, needle):
    return needle in ruleset

def test_generated_ruleset_valid():
    ruleset, tables = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353,
                                       vpn_ips=["151.101.65.7"])
    ok, msg = B.validate(ruleset)
    assert ok, "generated ruleset must parse: %s" % msg
    assert tables == ["nat", "filter"], tables

def test_redirect_targets_sinkhole_and_excludes_upstream():
    ruleset, _ = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353)
    assert "--to-ports 5353" in ruleset, "redirect must target sinkhole port"
    assert "--dst 127.0.0.53" in ruleset, "redirect must exclude upstream"
    # both udp and tcp port 53 are redirected
    assert "-p udp --dport 53 -j REDIRECT" in ruleset
    assert "-p tcp --dport 53 -j REDIRECT" in ruleset
    # the upstream is negated (excluded) for each protocol
    assert "! --dst 127.0.0.53 -p udp --dport 53 -j REDIRECT --to-ports 5353" in ruleset

def test_doh_servers_blocked_on_443():
    ruleset, _ = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353)
    for ip in B.DOH_ENDPOINTS["cloudflare"]:
        assert ("--dst %s/32 -p tcp --dport 443 -j DROP" % ip) in ruleset, ip
    assert "--dst 8.8.8.8/32 -p tcp --dport 443 -j DROP" in ruleset
    assert "--dst 9.9.9.9/32 -p tcp --dport 443 -j DROP" in ruleset

def test_vpn_peer_blocked():
    ruleset, _ = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353,
                                  vpn_ips=["151.101.65.7", "10.8.0.1"])
    assert "--dst 151.101.65.7/32 -j DROP" in ruleset
    assert "--dst 10.8.0.1/32 -j DROP" in ruleset

def test_doh_names_filtering():
    ruleset, _ = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353,
                                  doh_names=("cloudflare",))
    assert "--dst 1.1.1.1/32" in ruleset
    assert "8.8.8.8" not in ruleset, "google should be excluded when not requested"

def test_dry_run_returns_ruleset():
    ruleset, _ = B.generate_rules(uphost="127.0.0.53", sinkhole_port=5353)
    ok, msg = B.apply(ruleset, dry_run=True)
    assert ok, "dry-run should succeed"
    assert "dry-run" in msg and "COMMIT" in msg, msg

def test_is_root_is_bool():
    assert isinstance(B.is_root(), bool)

def test_detect_vpn_peers_returns_list():
    peers = B.detect_vpn_peers()
    assert isinstance(peers, list)
    for p in peers:
        assert "." in p, "peer should be an IP, got %r" % p

# ---------------- live detectors (root-free, pure where possible) ----------------
SAMPLE_ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRRT\n"
    "enp4s0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "enp4s0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
)

def test_proc_hex_to_ip_loopback():
    assert B._proc_hex_to_ip("0100007F:0050") == ("127.0.0.1", 80)

def test_proc_hex_to_ip_lan_gw():
    assert B._proc_hex_to_ip("0100A8C0:D431") == ("192.168.0.1", 54321)

def test_proc_hex_to_ip_unspecified():
    assert B._proc_hex_to_ip("00000000:0000") == ("0.0.0.0", 0)

def test_parse_proc_net_route_default_gw():
    gw, dev = B.parse_proc_net_route(SAMPLE_ROUTE)
    assert gw == "192.168.0.1" and dev == "enp4s0", (gw, dev)

def test_parse_proc_net_route_no_default():
    text = "Iface\tDestination\tGateway \tFlags\nenp4s0\t0000A8C0\t00000000\t0001\n"
    assert B.parse_proc_net_route(text) == (None, None)

def test_vpn_status_fake_tun_up():
    active, why = B.vpn_status(routes_text=SAMPLE_ROUTE, netdevs=["lo", "tun0"])
    assert active is True and why, (active, why)

def test_vpn_status_fake_clean():
    active, why = B.vpn_status(routes_text=SAMPLE_ROUTE,
                               netdevs=["enp4s0", "eth0"],
                               proc_names=["systemd", "firefox"])
    assert active is False and why is None, (active, why)

def test_vpn_status_fake_client_process():
    active, why = B.vpn_status(routes_text=SAMPLE_ROUTE, netdevs=["enp4s0"],
                               proc_names=["openvpn", "systemd"])
    assert active is True and why, (active, why)

def test_scan_public_doh_live_empty():
    peers, known = B.scan_public_doh()
    assert peers == [] and len(known) > 0, (peers, len(known))

def test_detect_shape_and_gateway():
    d = B.detect(lan_gateway="192.168.0.1", doh_port=8443)
    assert {"vpn", "gateway", "public_doh", "firefox", "off_dns", "gateway_changed"} <= set(d)
    assert d["gateway"] == "192.168.0.1", d["gateway"]
    assert d["gateway_changed"] is False
    assert d["vpn"]["active"] is False, d["vpn"]
    assert isinstance(d["firefox"]["pids"], list)
    assert isinstance(d["off_dns"]["active"], bool)
    assert isinstance(d["off_dns"]["peers"], list)

def test_firefox_off_gateway_dns_shape():
    active, peers = B.firefox_off_gateway_dns()
    assert isinstance(active, bool) and isinstance(peers, list)
    for p in peers:
        assert p.endswith(":53") or p.endswith(":443"), p

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print("ALL BYPASS TESTS PASSED")
