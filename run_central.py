#!/usr/bin/env python3
"""Control Limits - CENTRAL entrypoint (thin launcher).

First-class launcher for the *central* role: it holds all limits (the single
source of truth) and serves the admin UI/API only. Nobody browses through the
central, so the plain-DNS sinkhole, the DoH front-door, and the device-local
bypass watcher are all disabled.

Previously that meant hand-typing:

    python3 app.py --no-sinkhole --no-doh --no-bypass --web 8080 --db data/limits.db

which is easy to get wrong. This entrypoint wraps that exact flag set so the
"central vs single-host" split is explicit and hard to break. It re-runs the
shared `app.main()` entrypoint, so it is covered by the same behaviour the
test-suite exercises when it stands up a central-only `app.py`.

Usage:
    python3 run_central.py                     # :8080, data/limits.db
    python3 run_central.py --web 8080          # override admin port
    python3 run_central.py --db data/limits.db # override limits DB
"""
import argparse, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import app


def main():
    ap = argparse.ArgumentParser(
        description="Control Limits central (limits + admin only; no browsing front-door).")
    ap.add_argument("--web", type=int, default=int(os.environ.get("WEB_PORT", "8080")),
                    help="admin/web UI + API port")
    ap.add_argument("--db", default=os.environ.get("LIMITS_DB",
                    os.path.join(BASE, "data", "limits.db")),
                    help="limits sqlite DB path")
    args = ap.parse_args()

    # Re-run the shared gateway entrypoint, forcing central-only behaviour.
    # Mirrors the exact flag set the test-suite uses for the central.
    sys.argv = [
        "app.py",
        "--dohport", "0",
        "--no-sinkhole", "--no-doh", "--no-bypass",
        "--web", str(args.web),
        "--db", args.db,
    ]
    app.main()


if __name__ == "__main__":
    main()
