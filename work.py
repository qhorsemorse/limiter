"""Work / focus-session feature (server-side, calendar-day aware, cheat-resistant).

Compared against the workout, these are the deliberate differences:

  * The workout is a *hover* timer whose reward UN-blocks everything. The Work
    timer is a *button* (start / pause / resume) and, while a session is active
    (or a penalty is pending), it BLOCKS everything ("no blocked contents
    allowed") — both time-limits and absolute limits, regardless of any
    grace/allowance. Enforcement is driven off Store.set_work_until()/
    work_active().
  * The workout has one duration per day. Work has:
      - multiple *cards* (task kinds) that all share ONE common `limit_ms`
        (the minimum amount of real work-time required to finish the session);
      - a `budget_ms` ("the work timer"): the max time available for the
        session. It is predefined, can be set higher at any time, and can never
        be lowered below the largest value ever chosen (monotonic non-decreasing),
        just like the minimum. The session fails if the timer runs out before the
        minimum is reached.
  * The minimum `limit_ms` is established when the first work card is created
    ("the start of the card's life") and can only ever grow or stay the same on
    later days — never below its original value.

Rules:
  * A *session* is one day's block of focused work. It renews at LOCAL 00:00
    (accrued time + session state reset); the LIMITS (budget/minimum) persist and
    are monotonic.
  * The timer is button-driven, not hover. Pausing freezes the countdown and
    stops crediting; resuming continues. Credit is capped by REAL elapsed
    wall-clock time between ticks (same cheat-resistance as the workout) so the
    required minimum cannot be fast-forwarded by posting big deltas.
  * On SUCCESS (accrued reaches the minimum before the budget runs out) the block
    is cleared. On FAILURE (budget runs out first) a *penalty* window is armed:
    everything stays blocked for `penalty_ms`, then unlocks.

State persists to data/work.json (like workout.json) so gateway restarts don't
lose progress or a pending penalty/window.
"""
import json, os, threading, time

MIN_BUDGET_MS = int(os.environ.get("MIN_WORK_BUDGET_MS", 20 * 60 * 1000))  # timer floor (20m)
MIN_MIN_MS = int(os.environ.get("MIN_WORK_MIN_MS", 10 * 60 * 1000))        # minimum floor (10m)
DEFAULT_BUDGET_MS = int(os.environ.get("WORK_BUDGET_MS", 60 * 60 * 1000))   # default timer (60m)
DEFAULT_MIN_MS = int(os.environ.get("WORK_MIN_MS", 30 * 60 * 1000))         # default minimum (30m)
DEFAULT_PALTY_MS = int(os.environ.get("WORK_PALTY_MS", 30 * 60 * 1000))     # penalty on failure (30m)
MAX_MS = 8 * 3600 * 1000          # 8 h sanity cap for any time value
_MAX_DELTA_MS = 5000.0            # client sanity cap per single tick


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


class Work:
    def __init__(self, path):
        self.path = path
        # RLock on purpose: mutating methods return self.status() WHILE holding
        # the lock — a plain Lock would deadlock the handler thread.
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

    # ---------- calendar-day handling (shared shape with workout.py) ----------
    @staticmethod
    def _now_ms():
        return time.time() * 1000

    @classmethod
    def _today(cls, now_ms=None):
        now_ms = now_ms if now_ms is not None else cls._now_ms()
        return time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000.0))

    def _blank(self, date):
        return {
            "date": date,
            "session": "idle",
            "accrued_ms": 0.0,
            "active_card_id": None,
            "session_start_ms": None,
            "last_tick_ms": None,
        }

    def _maybe_reset(self):
        """Reset the per-day SESSION state at local 00:00 (and if accrued has
        somehow overrun the budget). The LIMITS (budget/limit) and the cards
        PERSIST — they are monotonic / long-lived, not daily state."""
        today = self._today()
        st = self.state
        budget = int(_clamp(st.get("budget_ms", DEFAULT_BUDGET_MS), MIN_BUDGET_MS, MAX_MS))
        if st.get("date") != today or (st.get("accrued_ms", 0.0) or 0.0) > budget:
            fresh = self._blank(today)
            fresh["budget_ms"] = budget           # keep monotonic budget
            fresh["limit_ms"] = st.get("limit_ms")
            fresh["penalty_ms"] = st.get("penalty_ms", DEFAULT_PALTY_MS)
            fresh["cards"] = st.get("cards", [])
            self.state = fresh
            self._save()

    # ---------- limit / budget floors (monotonic) ----------
    def _lim_floor(self):
        return max(MIN_MIN_MS, int(_clamp(self.state.get("limit_ms", DEFAULT_MIN_MS), MIN_MIN_MS, MAX_MS)))

    def _budget_floor(self):
        return max(MIN_BUDGET_MS, int(_clamp(self.state.get("budget_ms", DEFAULT_BUDGET_MS), MIN_BUDGET_MS, MAX_MS)))

    def _penalty(self):
        return int(_clamp(self.state.get("penalty_ms", DEFAULT_PALTY_MS), 0, MAX_MS))

    # ---------- "block everything" window (mirrors workout reward window) ----------
    def _active_until_locked(self, now):
        """Epoch ms until which everything must be blocked (0 = do NOT block),
        computed from the current session state. Includes a penalty buffer after
        failure so the block outlives the session even if the client stops ticking.
        CALLER MUST HOLD self.lock (this is invoked from within status())."""
        st = self.state
        session = st.get("session", "idle")
        budget = self._budget_floor()
        penalty = self._penalty()
        start = float(st.get("session_start_ms") or 0)
        if session in ("working", "paused"):
            # whole session window + penalty buffer (worst case: no more ticks)
            return int(start + budget + penalty)
        if session == "failed":
            # penalty window armed at failure; keep blocking until it clears
            armed = float(st.get("penalty_until_ms") or 0)
            return max(int(start + budget + penalty), int(armed))
        # success / idle -> nothing to block
        return 0

    # ---------- public api (called from app.py / secondary.py routes) ----------
    def status(self, now_ms=None):
        with self.lock:
            self._maybe_reset()
            st = self.state
            now = now_ms if now_ms is not None else self._now_ms()
            budget = self._budget_floor()
            limit = self._lim_floor()
            accrued = float(st.get("accrued_ms", 0.0))
            remaining = max(0.0, budget - accrued)
            session = st.get("session", "idle")
            penalty_until = float(st.get("penalty_until_ms") or 0)
            penalty_active = bool(penalty_until and now < penalty_until)
            return {
                "date": st["date"],
                "session": session,
                "budget_ms": budget,
                "limit_ms": limit,
                "penalty_ms": self._penalty(),
                "accrued_ms": int(accrued),
                "remaining_ms": int(remaining),
                "complete": session == "success",
                "failed": session == "failed",
                "penalty_active": penalty_active,
                "penalty_left_ms": int(max(0, penalty_until - now)),
                "cards": [dict(c) for c in st.get("cards", [])],
                "active_card_id": st.get("active_card_id"),
                # block-eom-thing window for the store to pick up (0 = off)
                "work_until_ms": self._active_until_locked(now),
            }

    def add_card(self, name, kind):
        with self.lock:
            self._maybe_reset()
            st = self.state
            name = (name or "").strip() or "(untitled work)"
            kind = (kind or "task").strip() or "task"
            cards = st.setdefault("cards", [])
            cid = (max([int(c.get("id", 0)) for c in cards]) + 1) if cards else 1
            cards.append({"id": cid, "name": name, "kind": kind, "accrued_ms": 0.0})
            # Establish the common minimum on the first card (start of card life).
            if st.get("limit_ms") is None:
                st["limit_ms"] = self._lim_floor()
            if not st.get("budget_ms"):
                st["budget_ms"] = self._budget_floor()
            self._save()
            return self.status()

    def remove_card(self, cid):
        with self.lock:
            self._maybe_reset()
            st = self.state
            cards = [c for c in st.get("cards", []) if int(c.get("id", -1)) != int(cid)]
            if len(cards) != len(st.get("cards", [])):
                st["cards"] = cards
                kept = {c["id"] for c in cards}
                if st.get("active_card_id") not in kept:
                    st["active_card_id"] = cards[0]["id"] if cards else None
                self._save()
            return self.status()

    def set_budget(self, new_ms):
        """Raise the work timer (budget). Monotonic: never below the largest
        chosen value. Meaningful at any time."""
        with self.lock:
            self._maybe_reset()
            cur = self._budget_floor()
            new_ms = int(_clamp(new_ms, MIN_BUDGET_MS, MAX_MS))
            self.state["budget_ms"] = max(cur, new_ms)
            self._save()
            return self.status()

    def set_limit(self, new_ms):
        """Raise the common minimum. Monotonic non-decreasing across days: can
        only grow, never below the original floor."""
        with self.lock:
            self._maybe_reset()
            cur = self._lim_floor()
            new_ms = int(_clamp(new_ms, MIN_MIN_MS, MAX_MS))
            self.state["limit_ms"] = max(cur, new_ms)
            self._save()
            return self.status()

    def set_penalty(self, new_ms):
        """Adjust the penalty window length (failure penalty). Monotonic-ish:
        always allowed; clamped to a sane floor."""
        with self.lock:
            self._maybe_reset()
            new_ms = int(_clamp(new_ms, 60 * 1000, MAX_MS))   # min 1 min
            self.state["penalty_ms"] = new_ms
            self._save()
            return self.status()

    def set_active_card(self, cid):
        with self.lock:
            self._maybe_reset()
            self.state["active_card_id"] = int(cid) if cid else None
            self._save()
            return self.status()

    def start(self):
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st.get("session") in ("success", "failed"):
                return self.status()
            now = self._now_ms()
            st["session"] = "working"
            st["session_start_ms"] = now
            st["last_tick_ms"] = now
            self._save()
            return self.status()

    def pause(self):
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st.get("session") == "working":
                st["session"] = "paused"
                st["last_tick_ms"] = self._now_ms()
                self._save()
            return self.status()

    def resume(self):
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st.get("session") == "paused":
                st["session"] = "working"
                st["last_tick_ms"] = self._now_ms()
                self._save()
            return self.status()

    def tick(self, delta_ms):
        """Credit real work-time (capped by REAL elapsed wall-clock). Returns
        (snapshot, error_string_or_None)."""
        now = self._now_ms()
        with self.lock:
            self._maybe_reset()
            st = self.state
            session = st.get("session")
            if session == "success":
                return self.status(), None
            if session not in ("working", "paused"):
                return self.status(), "work not started (call start first)"
            try:
                delta_ms = float(delta_ms)
            except (TypeError, ValueError):
                return self.status(), "bad delta_ms"
            delta_ms = max(0.0, min(delta_ms, _MAX_DELTA_MS))
            last = st.get("last_tick_ms") or now
            # Cheat-resistance: credit capped by REAL elapsed wall-clock since last
            # tick (same trick as workout.py — a hostile client can't fast-forward).
            credit = min(delta_ms, max(0.0, now - last))
            st["last_tick_ms"] = now

            budget = self._budget_floor()
            limit = self._lim_floor()
            st["accrued_ms"] = min(float(budget), float(st.get("accrued_ms", 0.0)) + credit)

            # attribute the delta to the focused card (fall back to the first one)
            cards = st.get("cards", [])
            target = next((c for c in cards if c.get("id") == st.get("active_card_id")), None)
            if target is None and cards:
                target = cards[0]
            if target is not None:
                target["accrued_ms"] = float(target.get("accrued_ms", 0.0)) + credit

            accrued = st["accrued_ms"]
            if accrued >= limit:
                # reached the common minimum -> SUCCESS (block cleared by caller)
                st["session"] = "success"
                st["penalty_until_ms"] = None
                self._save()
                return self.status(), None
            if accrued >= budget:
                # timer ran out before reaching the minimum -> FAILURE + penalty
                st["session"] = "failed"
                st["penalty_until_ms"] = now + self._penalty()
                self._save()
                return self.status(), None
            self._save()
            return self.status(), None
