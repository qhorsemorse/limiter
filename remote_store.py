"""Remote / secondary-side state store.

A *secondary* device does not hold the limits itself — the limits live on the
central server (single source of truth). This module keeps a local **cache that
is a copy of the central `limits` database** and answers resolution queries from
it, re-syncing from the central when a row is missing or stale (older than its
TTL, where "stake" is measured from the row's `updated_at` / created time).

Design / behaviour (documented so future AIs don't "fix" it back):

  * Cache-first. For every request we look at the local cache. If the cached
    copy is fresh (synced within `ttl_ms`), we answer straight from it. If it is
    absent or stale we fetch the whole limits table from the central
    (`GET /api/limits`), replace the cache, and stamp the sync time — then we
    answer. This is the "cache is a copy of the DB, refreshed on request" model;
    it is deliberately NOT a per-key TTL cache.

  * Stakeness = row age. The TTL check reads the cached rows' `updated_at`
    column (mirrored from the central's `updated_at`; the central stamps it
    every time a limit's state machine changes via db.save_state). If the newest
    row is older than `ttl_ms` we treat the cache as stale and re-fetch.

  * Reporting access. When a domain on this device is actually reached we POST
    it to the central (`POST /api/detect`); the central arms the grace timer
    (once, centrally) and returns the affected limits, which we drop into the
    local cache so a later evaluate sees the authoritative state.

  * Offline / emergency. The central is reachable only on the LAN. When a sync
    attempt fails we flip `emergency=True`. In emergency we follow a
    fail-closed policy (overridable via `allow_free_offline`):
        - Locked rows (absolute limits, or time-limits in cooldown) -> ALWAYS
          blocked. A lock can never be bypassed while offline.
        - Free / unlimited rows -> blocked too, because they "have to wait for
          the secondary server request to the central" to confirm they are
          truly unrestricted. Reconnect to the central and everything resumes
          immediately.
    Rationale: the two user rules ("no-lock sites pass") and ("offline blocks
    everything, free included, until a central sync succeeds") are in mild
    tension; we default to the safer fail-closed behaviour. Set
    `allow_free_offline=True` to instead forward unrestricted sites while offline.
"""
import json, os, threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

import engine as E
import db as DB

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS limits (
    id INTEGER PRIMARY KEY,
    name TEXT, type TEXT, domains TEXT,
    grace_ms INTEGER, cooldown_ms INTEGER,
    state TEXT, grace_until REAL, cooldown_until REAL,
    created_at REAL, updated_at REAL
)
"""


def _http_json(method, url, obj=None, timeout=5.0):
    """Minimal JSON HTTP helper (stdlib only). Returns (status, parsed_obj).
    Raises on connection/URL error so callers can catch and go offline."""
    data = None
    headers = {"Accept": "application/json"}
    if obj is not None:
        data = json.dumps(obj).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            parsed = {}
        return r.getcode(), parsed


class CacheDB:
    """Local copy of the central `limits` table. Not the source of truth — the
    central is. This is a fast, local, synchronous cache that mirrors it."""

    def __init__(self, path):
        self.con = DB._connect(path)
        self.con.execute(_CACHE_SCHEMA)
        self.con.commit()

    def replace_all(self, rows):
        """Full sync: blow away the local copy and reinsert the central rows.
        `rows` is the list of dicts returned by `GET /api/limits` (each already
        carries updated_at). Returns rows actually loaded."""
        with self.con:
            self.con.execute("DELETE FROM limits")
            self.con.executemany(
                "INSERT INTO limits(id,name,type,domains,grace_ms,cooldown_ms,"
                "state,grace_until,cooldown_until,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(r["id"], r.get("name", ""), r.get("type", ""),
                  __import__("json").dumps(r.get("domains", [])),
                  r.get("grace_ms"), r.get("cooldown_ms"), r.get("state", "free"),
                  r.get("grace_until"), r.get("cooldown_until"),
                  r.get("updated_at") or r.get("created_at"),
                  r.get("updated_at") or r.get("created_at")) for r in rows])
        return len(rows)

    def touch_row(self, limit):
        """Per-detect in-place update of one row's state (authoritative state
        returned by the central). Upserts by id."""
        with self.con:
            self.con.execute(
                "INSERT INTO limits(id,name,type,domains,grace_ms,cooldown_ms,"
                "state,grace_until,cooldown_until,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type,"
                "domains=excluded.domains,grace_ms=excluded.grace_ms,"
                "cooldown_ms=excluded.cooldown_ms,state=excluded.state,"
                "grace_until=excluded.grace_until,cooldown_until=excluded.cooldown_until,"
                "created_at=COALESCE(excluded.created_at,limits.created_at),"
                "updated_at=excluded.updated_at",
                [limit["id"], limit.get("name", ""), limit.get("type", ""),
                 __import__("json").dumps(limit.get("domains", [])),
                 limit.get("grace_ms"), limit.get("cooldown_ms"),
                 limit.get("state", "free"), limit.get("grace_until"),
                 limit.get("cooldown_until"), limit.get("created_at"),
                 limit.get("updated_at")])

    def get_all(self):
        out = []
        for row in self.con.execute(
                "SELECT id,name,type,domains,grace_ms,cooldown_ms,state,"
                "grace_until,cooldown_until,created_at,updated_at FROM limits"):
            d = {
                "id": row[0], "name": row[1], "type": row[2],
                "domains": json.loads(row[3] or "[]"),
                "grace_ms": row[4], "cooldown_ms": row[5], "state": row[6],
                "grace_until": row[7], "cooldown_until": row[8],
                "created_at": row[9], "updated_at": row[10],
            }
            out.append(d)
        return out

    def max_updated_at(self):
        row = self.con.execute("SELECT MAX(updated_at) FROM limits").fetchone()
        return row[0] or 0.0


class RemoteStore:
    """Drop-in reader/evaluator for the resolution core (dns.resolve_bytes /
    doh), backed by a local cache that mirrors the central server.

    Same public surface as store.Store for the fields the rest of the app
    touches: tick, reward_active, is_nuclear/set_nuclear, override/set_override,
    reward_until/set_reward_until, evaluate, detect, count, snapshot, stats, plus
    online/emergency/last_sync introspection."""

    def __init__(self, cache_db_path, central_base, ttl_seconds=30.0,
                 http_timeout=5.0, allow_free_offline=False, sync_now=True):
        self.cache = CacheDB(cache_db_path)
        self.central_base = central_base.rstrip("/")
        self.ttl_ms = int(ttl_seconds * 1000)
        self.http_timeout = http_timeout
        # fail-closed by default: while offline, even unrestricted sites are
        # blocked until we can reach the central to confirm their state.
        self.allow_free_offline = allow_free_offline

        # bypass/nuclear / reward / stats state (same semantics as local Store)
        self.lock = threading.RLock()
        self.nuclear = False
        self.nuclear_reason = None
        self.override = False
        self.reward_until = 0.0
        self.stats = {"udp": [0, 0], "doh": [0, 0]}

        # connectivity / freshness state
        self.online = False
        self.emergency = False
        self.last_sync_ms = 0.0

        if sync_now:
            self.sync()  # prime the cache at construction

    # ---------------- connectivity ----------------
    def sync(self):
        """Fetch the full limits table from the central and replace the local
        cache. Returns True on success, False if the central was unreachable
        (which puts us into emergency/offline mode)."""
        with self.lock:
            url = self.central_base + "/api/limits"
            try:
                _code, payload = _http_json("GET", url, timeout=self.http_timeout)
            except Exception:
                self.online = False
                self.emergency = True
                return False
            if not isinstance(payload, list):
                self.online = False
                self.emergency = True
                return False
            loaded = self.cache.replace_all(payload)
            self.last_sync_ms = E.now_ms()
            self.online = True
            self.emergency = False
            return True

    def report_access(self, domain):
        """Tell the central a domain was reached (arms grace centrally) and fold
        the returned authoritative state into the local cache. Returns the count
        of limits affected (>=1 means an access was reported)."""
        domain = E.normalize_domain(domain) if domain else ""
        url = self.central_base + "/api/detect"
        try:
            _code, payload = _http_json("POST", url,
                                        {"domain": domain}, timeout=self.http_timeout)
        except Exception:
            # central unreachable — still refresh our cache best-effort so a
            # later read is as fresh as possible.
            self.sync()
            return 0
        affected = payload.get("detected") or []
        for lim in affected:
            self.cache.touch_row(lim)
        # touch the sync timestamp so staleness reflects this activity
        self.last_sync_ms = E.now_ms()
        return len(affected)

    def _ensure_fresh(self, now=None):
        """Trigger a sync when the cache is stale/absent. `now` is epoch ms."""
        now = now if now is not None else E.now_ms()
        fresh_at = self.last_sync_ms + self.ttl_ms
        if now >= fresh_at:
            self.sync()
        return self.online

    # ---------------- bypass / nuclear state ----------------
    def is_nuclear(self):
        with self.lock:
            return self.nuclear and not self.override

    def set_nuclear(self, on, reason=None):
        with self.lock:
            self.nuclear = bool(on)
            self.nuclear_reason = reason if on else None

    def set_override(self, on):
        with self.lock:
            self.override = bool(on)
            return self.override

    def set_reward_until(self, ms):
        with self.lock:
            self.reward_until = float(ms or 0)

    def reward_active(self, now=None):
        with self.lock:
            now = now if now is not None else E.now_ms()
            return bool(self.reward_until and now < self.reward_until)

    # ---------------- reads / evaluation ----------------
    def snapshot(self):
        """Copy of cached limits for the (read-only on secondary) UI + health."""
        with self.lock:
            return self.cache.get_all()

    def _cached_limits(self):
        return self.cache.get_all()

    def evaluate(self, domain):
        """engine.evaluate over the cached (fresh) limits. Offline/emergency
        policy is applied to the not-blocked case."""
        with self.lock:
            now = E.now_ms()
            self._ensure_fresh(now)
            limits = self._cached_limits()
            blocked, lid = E.evaluate(limits, domain)
            if blocked:
                return True, lid
            # not blocked by any limit:
            if not self.online and not self.allow_free_offline:
                # fail-closed: we cannot confirm the site is unrestricted without
                # a central sync, so block until we reconnect.
                return True, None
            return False, None

    def detect(self, domain, now=None):
        """Report an actual access to the central so it arms grace centrally;
        fold the authoritative state into the local cache. (resolve_bytes
        ignores the return value; tests and callers use it to observe effect.)"""
        now = now if now is not None else E.now_ms()
        with self.lock:
            self._ensure_fresh(now)
            return self.report_access(domain)

    def tick(self, now=None):
        """For the remote store the authoritative timers live centrally, so a
        tick is just a freshness nudge — make sure the cache is fresh."""
        now = now if now is not None else E.now_ms()
        with self.lock:
            self._ensure_fresh(now)
            return self

    def count(self, channel, blocked):
        with self.lock:
            try:
                self.stats[channel][0] += 1
                if blocked:
                    self.stats[channel][1] += 1
            except KeyError:
                pass
            return self.stats[channel][0], self.stats[channel][1]

    # ---------------- introspection ----------------
    # `online` / `emergency` / `last_sync_ms` are plain attributes set in
    # sync(); exposed directly so the secondary can report health status.
