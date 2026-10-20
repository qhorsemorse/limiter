"""Workout -> reward feature (server-side, calendar-day aware, cheat-resistant).

Rules (per user spec):
  * ONE workout per calendar day. It renews at LOCAL 00:00 of the next day
    (not a 24h sliding window).
  * The workout is a hover-timer: the client only counts while the cursor is
    over the timer. Every chunk of hover time is reported here and CREDIT IS
    CAPPED BY REAL ELAPSED WALL-CLOCK TIME, so the required duration cannot be
    fast-forwarded by simply POSTing big numbers.
  * Workout duration: hard floor of 25 MINUTES (MIN_WORKOUT_MS). Longer
    durations can be selected via the UI (set_duration), but ONLY before the
    first hover of the day; once started the choice is locked for the rest of
    the day, and the next day resets to the 25-minute default.
  * REWARD = a no-block pass lasting  sqrt(x) * ln(x) * 0.9  MINUTES, where
    x = the (selected) workout duration in minutes — a hardcoded ratio (e.g.
    a 25-min workout -> ~14.5-min pass; 60 -> ~20-min; 120 -> ~25.6-min).
  * The reward exists ONLY if the workout runs to ZERO. An abandoned or
    partial session (even 10 minutes short) earns nothing.
  * The reward is non-stackable (one redemption per day) and same-day only:
    if not redeemed before 00:00 it is gone; a redeemed pass cannot be
    claimed again that day.

State persists to data/workout.json so gateway restarts don't erase progress
or a pending/active reward.
"""
import json, math, os, threading, time

MIN_WORKOUT_MS = int(os.environ.get("MIN_WORKOUT_MS", 25 * 60 * 1000))   # hard floor: 25 min
MAX_WORKOUT_MS = 8 * 3600 * 1000                                         # sanity cap (8 h)
DEFAULT_WORKOUT_MS = max(int(os.environ.get("WORKOUT_SECONDS", 25 * 60)) * 1000, MIN_WORKOUT_MS)
_MAX_DELTA_MS = 5000.0            # client sanity cap per single tick


def reward_ms_for(duration_ms):
    """Hardcoded ratio. x = workout duration in MINUTES ->
    reward = sqrt(x) * ln(x) * 0.9 minutes, in ms.
    (In the real domain x >= 25 the value is always positive; the 1 s floor
    only guards degenerate test configurations.)"""
    x = max(1e-9, float(duration_ms) / 60000.0)
    return max(1000.0, float(math.sqrt(x) * math.log(x) * 0.9) * 60000.0)


class Workout:
    def __init__(self, path):
        self.path = path
        # RLock on purpose: start/tick/redeem/set_duration all return
        # self.status() WHILE holding the lock — a plain Lock deadlocks the
        # handler thread (and poisons every later call that waits on it).
        self.lock = threading.RLock()
        self.state = self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            with open(self.path) as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except Exception:
            pass
        return {}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- calendar-day handling ----------
    @staticmethod
    def _now_ms():
        return time.time() * 1000

    @classmethod
    def _today(cls, now_ms=None):
        now_ms = now_ms if now_ms is not None else cls._now_ms()
        return time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000.0))

    def _dur(self, st):
        """The workout duration selected for THIS calendar day (ms)."""
        try:
            return max(MIN_WORKOUT_MS, min(MAX_WORKOUT_MS, int(st.get("duration_ms") or DEFAULT_WORKOUT_MS)))
        except (TypeError, ValueError):
            return DEFAULT_WORKOUT_MS

    def _blank(self, date):
        return {
            "date": date,
            "accrued_ms": 0.0,
            "duration_ms": DEFAULT_WORKOUT_MS,
            "started": False,
            "complete": False,
            "completed_at_ms": None,
            "redeemed": False,
            "reward_until_ms": None,
            "last_tick_ms": None,
        }

    def _maybe_reset(self):
        """Reset at local 00:00 — the ONLY place daily renewal happens.
        (Duration reverts to the 25-min default with the fresh day.)"""
        today = self._today()
        st = self.state
        if st.get("date") != today or st.get("accrued_ms", 0) > self._dur(st):
            self.state = self._blank(today)
            self._save()

    # ---------- public api (called from app.py routes) ----------
    def status(self, now_ms=None):
        with self.lock:
            self._maybe_reset()
            now = now_ms if now_ms is not None else self._now_ms()
            st = self.state
            req = self._dur(st)
            until = st.get("reward_until_ms") or 0
            return {
                "date": st["date"],
                "duration_ms": req,
                "accrued_ms": int(min(st.get("accrued_ms", 0.0), req)),
                "required_ms": req,
                "remaining_ms": int(max(0, req - st.get("accrued_ms", 0.0))),
                "started": st.get("started", False),
                "complete": st.get("complete", False),
                "completed_at_ms": st.get("completed_at_ms"),
                "redeemed": st.get("redeemed", False),
                # reward for the CURRENT day's selected duration (preview + at redeem)
                "reward_preview_ms": int(reward_ms_for(req)),
                "reward_available": bool(st.get("complete")) and not st.get("redeemed"),
                "reward_until_ms": st.get("reward_until_ms"),
                "reward_active": bool(until > now),
                "reward_left_ms": int(max(0, until - now)),
            }

    def start(self):
        with self.lock:
            self._maybe_reset()
            if not self.state["complete"]:
                self.state["started"] = True
                self.state["last_tick_ms"] = self._now_ms()
                self._save()
            return self.status()

    def set_duration(self, new_ms):
        """Pick a longer workout for today. Only before the first hover;
        clamped to [MIN_WORKOUT_MS, MAX_WORKOUT_MS]. Returns (snapshot, err)."""
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st["complete"]:
                return self.status(), "workout already completed today"
            if st.get("started"):
                return self.status(), "already started today — duration is locked until 00:00"
            try:
                new_ms = float(new_ms)
            except (TypeError, ValueError):
                return self.status(), "bad duration value"
            if new_ms < MIN_WORKOUT_MS:
                return self.status(), "workout must be at least %d minutes" % (MIN_WORKOUT_MS // 60000)
            st["duration_ms"] = min(int(new_ms), MAX_WORKOUT_MS)
            self._save()
            return self.status(), None

    def tick(self, delta_ms):
        """Credit hover-time. Capped by REAL elapsed wall-clock time.
        Returns (snapshot, error_string_or_None)."""
        now = self._now_ms()
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st["complete"]:
                return self.status(), None
            if not st.get("started"):
                return self.status(), "workout not started (call start first)"
            try:
                delta_ms = float(delta_ms)
            except (TypeError, ValueError):
                return self.status(), "bad delta_ms"
            delta_ms = max(0.0, min(delta_ms, _MAX_DELTA_MS))
            last = st.get("last_tick_ms") or now
            # Credit is capped by REAL server-side elapsed time since the last
            # tick. (Do NOT grant a per-tick epsilon here: with fast ticks it
            # accumulates and a hostile client could farm ~11x credit speed.
            # Honest clients lose nothing, because a late report is still
            # credited up to its full reported delta.)
            credit = min(delta_ms, max(0.0, now - last))
            st["last_tick_ms"] = now
            req = self._dur(st)
            st["accrued_ms"] = min(req, st.get("accrued_ms", 0.0) + credit)
            if st["accrued_ms"] >= req:
                st["complete"] = True
                st["completed_at_ms"] = now
            self._save()
            return self.status(), None

    def redeem(self, store=None):
        """Claim the no-block pass (duration = sqrt(x)*ln(x)*0.9 min).
        One per day, same-day only, and only after a FULL completion.
        Returns (snapshot, error_string_or_None)."""
        now = self._now_ms()
        with self.lock:
            self._maybe_reset()
            st = self.state
            if not st.get("complete"):
                return self.status(), "workout not completed (a partial session earns no reward)"
            if st.get("redeemed"):
                return self.status(), "reward already redeemed today (non-stackable)"
            st["redeemed"] = True
            st["reward_until_ms"] = now + reward_ms_for(self._dur(st))
            self._save()
            if store is not None and hasattr(store, "set_reward_until"):
                store.set_reward_until(st["reward_until_ms"])
            return self.status(), None
