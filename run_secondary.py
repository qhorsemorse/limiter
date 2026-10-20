#!/usr/bin/env python3
"""Control Limits - SECONDARY entrypoint (thin launcher).

First-class launcher for the *secondary* (child-device) role: it runs
`secondary.py` — the device's DoH front-door + device-local bypass watcher +
sync ticker against the central — with a clean, documented argument set.

Keeps the secondary role symmetric with run_central.py. It is equivalent to
running `secondary.py` directly; the only point is to make "this is the
secondary" explicit and to share one launcher style across both roles.

Usage:
    python3 run_secondary.py --central http://192.168.0.10:8080
    python3 run_secondary.py --central http://192.168.0.10:8080 --web 8080
    python3 run_secondary.py                 # reads data/secondary.json
"""
import argparse, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import secondary


def main():
    ap = argparse.ArgumentParser(
        description="Control Limits secondary (child device) gateway.")
    ap.add_argument("--central", default=None, help="central base URL (overrides config/env)")
    ap.add_argument("--web", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--dohport", type=int, default=int(os.environ.get("DOH_PORT", "8443")))
    ap.add_argument("--certdir", default=os.environ.get("CERTDIR", os.path.join(BASE, "certs")))
    ap.add_argument("--uphost", default=os.environ.get("DNS_UP", "127.0.0.53"))
    ap.add_argument("--upport", type=int, default=53)  # (secondary.py spells it --upport)
    ap.add_argument("--lan-gw", default=os.environ.get("LAN_GW", ""))
    ap.add_argument("--ttl", type=float, default=float(os.environ.get("REMOTE_TTL", "30")))
    ap.add_argument("--bypass-interval", type=float, default=5.0)
    ap.add_argument("--allow-free-offline", dest="allow_free_offline",
                    action="store_true",
                    help="while offline, forward unrestricted sites (fail-open; default is fail-closed)")
    args = ap.parse_args()

    sys.argv = [
        "secondary.py",
        "--central", args.central or "",
        "--web", str(args.web),
        "--dohport", str(args.dohport),
        "--certdir", args.certdir,
        "--uphost", args.uphost,
        "--upport", str(args.upport),
        "--ttl", str(args.ttl),
    ]
    if args.lan_gw:
        sys.argv += ["--lan-gw", args.lan_gw]
    if args.allow_free_offline:
        sys.argv.append("--allow-free-offline")
    secondary.main()


if __name__ == "__main__":
    main()
