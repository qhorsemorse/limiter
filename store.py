"""In-memory state bridge: engine logic <-> sqlite, shared across threads.
Thread-safe via a lock. Persisted to db on add/state-change."""
import threading, time
import engine as E, db as DB

class Store:
    def __init__(self, path=DB.DEFAULT_DB):
        self.con = DB.init_db(path)
        self.lock = threading.RLock()
        self.limits = DB.list_limits(self.con)
        # bypass/nuclear state + query counters (shared with http endpoints)
        self.nuclear = False
        self.nuclear_reason = None
        self.override = False
        self.reward_until = 0.0         # ms; workout reward window (all blocks off)
        self.stats = {"udp": [0, 0], "doh": [0, 0]}   # [total, blocked]

    # ---- bypass / nuclear state ----
    def set_nuclear(self, on, reason=None):
        with self.lock:
            self.nuclear = bool(on)
            self.nuclear_reason = reason if on else None

    def is_nuclear(self):
        with self.lock:
            # a parent override ("I dealt with it") silences the nuclear block
            return self.nuclear and not self.override

    def set_override(self, on):
        with self.lock:
            self.override = bool(on)
            return self.override

    def set_reward_until(self, ms):
        """Workout reward window: while active, resolve_bytes forwards
        everything (limits AND nuclear suspended)."""
        with self.lock:
            self.reward_until = float(ms or 0)

    def reward_active(self, now=None):
        with self.lock:
            now = now if now is not None else E.now_ms()
            return bool(self.reward_until and now < self.reward_until)

    # ---- writes ----
    def add(self, rec):
        with self.lock:
            lid = DB.add_limit(self.con, {
                "name": rec.get("name",""),
                "type": rec["type"],
                "domains": E.valid_domains(rec.get("domains", [])),
                "grace_ms": E._ms(rec.get("grace_min", 40)) if "grace_min" in rec else int(rec.get("grace_ms", 2_400_000)),
                "cooldown_ms": E._ms(rec.get("cooldown_min", 120)) if "cooldown_min" in rec else int(rec.get("cooldown_ms", 7_200_000)),
            })
            new = DB.get_limit(self.con, lid)
            self.limits.append(new)
            return new

    def delete(self, lid):
        with self.lock:
            n = DB.delete_limit(self.con, lid)
            if n:
                self.limits = [l for l in self.limits if l["id"] != lid]
        return bool(n)

    # ---- reads / evaluation ----
    def snapshot(self):
        with self.lock:
            return [dict(l) for l in self.limits]

    def current(self, limit_id):
        with self.lock:
            for l in self.limits:
                if l["id"] == limit_id:
                    return dict(l)
        return None

    def tick(self, now=None):
        now = now or E.now_ms()
        with self.lock:
            # persist timers that just changed
            for l in self.limits:
                if l["type"] != "time":
                    continue
                DB.save_state(self.con, l["id"], l["state"], l.get("grace_until"), l.get("cooldown_until"))
            E.tick(self.limits, now)

    def detect(self, domain, now=None):
        """Record that `domain` was reached; arm any matching free time-limit."""
        now = now or E.now_ms()
        with self.lock:
            hit = False
            for l in self.limits:
                if l["type"] == "time" and domain in l["domains"] and l["state"] == "free" and l.get("grace_until") is None:
                    E.detect_access(l, now)
                    hit = True
            if hit:
                for l in self.limits:
                    if l["type"] == "time":
                        DB.save_state(self.con, l["id"], l["state"], l.get("grace_until"), l.get("cooldown_until"))
        return hit

    def evaluate(self, domain):
        with self.lock:
            return E.evaluate(self.limits, domain)

    def report_access(self, domain):
        """Remote-callable counterpart to `detect`: `domain` was actually
        reached on a secondary device. Arm the grace timer on any matching
        free time-limit (once, centrally) and persist. Returns the list of
        affected limits as dicts so the caller/secondaries can refresh their
        cache with the authoritative state."""
        domain = E.normalize_domain(domain) if domain else ""
        now = E.now_ms()
        affected = []
        with self.lock:
            hit = False
            for l in self.limits:
                if (l["type"] == "time" and domain in l["domains"]
                        and l["state"] == "free" and l.get("grace_until") is None):
                    E.detect_access(l, now)
                    DB.save_state(self.con, l["id"], l["state"],
                                  l.get("grace_until"), l.get("cooldown_until"))
                    affected.append(dict(l))
                    hit = True
            # Also nudge every time-limit so its cooldown/grace timestamps in
            # the response are fresh for the secondary's cache.
            E.tick(self.limits, now)
            for l in self.limits:
                if l["type"] == "time":
                    DB.save_state(self.con, l["id"], l["state"],
                                  l.get("grace_until"), l.get("cooldown_until"))
        return affected

    def count(self, channel, blocked):
        with self.lock:
            try:
                self.stats[channel][0] += 1
                if blocked:
                    self.stats[channel][1] += 1
            except KeyError:
                pass
        return self.stats[channel][0], self.stats[channel][1]
