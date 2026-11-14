"""Work / focus-session feature (server-side, calendar-day aware, cheat-resistant).

CONCEPT (v7 — pomodoro goal timer with chimes, no self-locking):

  * You set a DAILY GOAL: the amount of real work-time (ms) you want to reach
    in one day ("I will work 5 hours today"). Set via the UI or
    POST /api/work {action:"set_goal", ms}. The setpoint is stored in
    data/work.json and persists across days; the per-day progress resets at
    local 00:00.
  * You set the POMODORO stretches: the work-stretch length and the
    rest-stretch length (defaults 50 min / 10 min). Set via the UI or
    POST /api/work {action:"set_interval", work_ms, rest_ms}. Also persisted.
  * Start runs ONE work stretch; the rhythm is server-anchored and chime-marked:
      - WORK stretch: the gateway blocks EVERY domain (focus mode — there is
        nowhere to be distracted). Only real time inside work stretches is
        credited to the goal ("subtracted" from it).
      - The work is done (stretch end): the REST stretch starts AUTOMATICALLY.
        The client plays the "time for a break" chime on this transition.
      - REST stretch: the block is OFF (you may genuinely rest / browse).
        Rest minutes are recorded (rested_ms) but never credited to the goal.
      - The break is over (rest end): the session stops in WAITING. The next
        work stretch does NOT start automatically — the client plays the
        "break over" chime and keeps LOOPING it until you RESUME (which starts
        a fresh work stretch) or PAUSE (which silences the waiting state; a
        later resume starts the work stretch then).
    So the day's rhythm is: [START] work → break → (chime loop) work → break
    → (chime loop) … — every work stretch is started deliberately.
  * GOAL REACHED (accrued work >= goal) -> session SUCCESS: the block clears,
    you are done for the day (the client plays a chime on this transition too).
  * A day that ENDS (local 00:00) with the goal NOT reached is simply LOGGED
    as a failed day in the work data — and a fresh IDLE day starts. The timer
    is NEVER locked by this: there is no failure state and no penalty window
    that blocks the timer or the apps (removed in v7 — the penalty was a
    mistaken design; a missed goal must not hold you).
  * WORK DATA: every day is recorded in the "log" list inside data/work.json:
      {"date","goal_ms","worked_ms","rested_ms","cycles","done","failed"}
    It is stored for now and included in GET /api/work; a dedicated progress
    display UI may come later.

Cheat resistance (same philosophy as the workout):
  * credited work/rest time is capped by REAL elapsed wall-clock between
    ticks — a hostile client cannot fast-forward by posting big deltas.
  * phase boundaries are anchored to the server wall clock (a phase always
    ends at the moment it should), so the cycle rhythm neither drifts nor
    shifts when the client lags, closes the tab or the gateway restarts.
    status() (and every action) auto-advances phases that expired in real
    time, so the resolver's block window follows the real rhythm. WAITING and
    PAUSED deliberately never auto-advance (they sit until the user acts).

States: idle -> working -> resting -> waiting -> (resume) working -> ...
        -> success
("paused" freezes the current phase — remaining phase time is remembered; a
 pause while WAITING just silences it — the block stays off the whole time).

Compared against the workout, the deliberate differences:
  * The workout is a *hover* timer whose reward UN-blocks everything. The Work
    timer is a *button* (start / pause / resume) that BLOCKS everything during
    work stretches ("no blocked contents allowed") — the enforcement is driven
    off Store.set_work_until()/work_active().
  * The workout has one duration per day. Work has a daily goal (settable) plus
    work/rest pomodoro lengths (settable), shared work *cards* (task kinds) for
    attributing the credited time, and a per-day work-data log.

State persists to data/work.json (like workout.json) so gateway restarts don't
lose progress, the log or a pending phase.

CHANGELOG v6 -> v7 (user request, 2026-11-14):
  * REMOVED the "day-end failure / penalty" self-lock: when 00:00 catches the
    session active with the goal unmet, the day is only LOGGED as failed and
    the new day starts IDLE. No "failed" session exists anymore, there is no
    penalty block window and no "set_penalty" action — a missed goal never
    blocks the timer or the apps. Legacy state files stuck in the old "failed"
    session are healed to "idle" on load.
  * REST no longer auto-chains into the next WORK stretch: it ends in the new
    "waiting" state. The work.html client plays/loops the "break over" chime
    there until the user resumes (fresh work stretch) or pauses (silence).
    The status exposes "awaiting_work" (true in waiting, or in a paused
    waiting) so the UI knows to ring.
  * The "work over" (working -> resting, or working -> success) and "break
    over" (resting -> waiting) transitions are the chime points the UI listens
    for; the server just flips states, the browser does the ringing.
"""
import json, os, threading, time

MIN_GOAL_MS = int(os.environ.get("MIN_WORK_GOAL_MS", 5 * 60 * 1000))             # goal floor (5m)
DEFAULT_GOAL_MS = int(os.environ.get("WORK_GOAL_MS", 300 * 60 * 1000))           # default goal (5h)
DEFAULT_WORK_MS = int(os.environ.get("WORK_INTERVAL_MS", 50 * 60 * 1000))        # default work stretch (50m)
DEFAULT_REST_MS = int(os.environ.get("WORK_REST_MS", 10 * 60 * 1000))            # default rest stretch (10m)
MIN_WORK_MS = int(os.environ.get("MIN_WORK_INTERVAL_MS", 60 * 1000))             # work stretch floor (1m)
MAX_PHASE_MS = int(os.environ.get("MAX_WORK_PHASE_MS", 4 * 3600 * 1000))         # per-stretch cap (4h)
MAX_MS = 8 * 3600 * 1000          # 8 h sanity cap for any time value
_MAX_DELTA_MS = 5000.0            # client sanity cap per single tick
_MAX_ADVANCE_STEPS = 200          # catch-up loop cap (tiny stretches + huge gap)
_VALID_SESSIONS = ("idle", "working", "resting", "waiting", "paused", "success")


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
                # one-way migration from the old (v5) budget/minimum concept:
                # the old "common minimum" becomes the daily GOAL; the old max
                # timer (budget) no longer exists in the pomodoro design.
                if st.get("goal_ms") is None and st.get("limit_ms"):
                    st["goal_ms"] = st["limit_ms"]
                st.pop("limit_ms", None)
                st.pop("budget_ms", None)
                # one-way migration from the v6 "day-end failure" self-lock
                # (removed in v7): a stuck "failed" session must never hold the
                # timer — heal it to idle — and drop the legacy penalty fields.
                # (The day it belongs to still gets its "failed" data flag via
                # day_ended_active when _maybe_reset finalizes it.)
                if st.get("session") not in _VALID_SESSIONS:
                    st["session"] = "idle"
                    st["day_ended_active"] = True
                if st.get("session") != "paused":
                    st.pop("wait_work", None)
                st.pop("penalty_until_ms", None)
                st.pop("penalty_ms", None)
                st.setdefault("log", [])
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

    @staticmethod
    def _midnight_after(now_ms):
        """The NEXT local 00:00 strictly after now, in ms. Used as a
        conservative 'block until the day ends' (paused inside a work
        stretch)."""
        t = now_ms / 1000.0
        y, m, d = time.localtime(t)[:3]
        nxt = time.mktime((y, m, d, 0, 0, 0, 0, 0, 0))
        if nxt <= t:
            nxt += 86400
        return int(nxt * 1000)

    def _blank(self, date):
        return {
            "date": date,
            "session": "idle",          # idle|working|resting|waiting|paused|success
            "phase": "work",            # the phase the (running or paused) cycle is in
            "phase_end_ms": None,       # wall ms at which the current phase expires
            "phase_left_ms": None,      # remaining ms of the phase (only while paused)
            "wait_work": None,          # True: this pause happened while WAITING for work
            "accrued_ms": 0.0,          # real WORK minutes credited to the goal today
            "rested_ms": 0.0,           # real REST minutes spent today
            "cycles": 0,                # work stretches completed today
            "session_start_ms": None,
            "last_tick_ms": None,
            "goal_ms": DEFAULT_GOAL_MS,
            "work_ms": DEFAULT_WORK_MS,
            "rest_ms": DEFAULT_REST_MS,
        }

    def _log_day(self, date, goal, worked, rested, cycles, done, failed):
        """Append (or refresh) that day's record in the persisted work-data log.
        This is the stored 'work data' a future progress UI will display.
        CALLER MUST HOLD self.lock."""
        rec = {"date": date, "goal_ms": int(goal), "worked_ms": int(worked),
               "rested_ms": int(rested), "cycles": int(cycles),
               "done": bool(done), "failed": bool(failed)}
        log = self.state.setdefault("log", [])
        for i, e in enumerate(log):
            if e.get("date") == date:
                log[i] = rec
                return
        log.append(rec)

    def _maybe_reset(self, now=None):
        """At local 00:00: finalize the previous day in the log and start a
        fresh IDLE day. A day that ended with the goal unmet is ONLY logged as
        failed (stored work data) — v7 has deliberately NO failure state and no
        penalty: a missed goal must never lock the timer or block the apps, so
        the new day is always startable right away. The SETPOINTS (goal /
        work & rest stretches) and the cards PERSIST — they are long-lived, not
        daily state."""
        now = now if now is not None else self._now_ms()
        today = self._today(now)
        st = self.state
        if st.get("date") == today:
            return
        prev = st.get("date")
        if prev:
            # finalize the day that just ended (a missed goal stays a stored
            # fact in the log — it changes nothing about the new day)
            ended_active = st.get("session") in ("working", "resting", "paused", "waiting") \
                or bool(st.pop("day_ended_active", False))
            self._log_day(date=prev, goal=self._goal(st),
                          worked=float(st.get("accrued_ms", 0.0)),
                          rested=float(st.get("rested_ms", 0.0)),
                          cycles=int(st.get("cycles", 0)),
                          done=st.get("session") == "success",
                          failed=ended_active
                                  and float(st.get("accrued_ms", 0.0)) < self._goal(st))
        fresh = self._blank(today)
        fresh["goal_ms"] = st.get("goal_ms", fresh["goal_ms"])
        fresh["work_ms"] = st.get("work_ms", fresh["work_ms"])
        fresh["rest_ms"] = st.get("rest_ms", fresh["rest_ms"])
        fresh["cards"] = st.get("cards", [])
        fresh["active_card_id"] = st.get("active_card_id")
        fresh["log"] = st.get("log", [])
        self.state = fresh
        self._save()

    # ---------- setpoints (clamped) ----------
    def _goal(self, st=None):
        st = st if st is not None else self.state
        return max(MIN_GOAL_MS, int(_clamp(st.get("goal_ms", DEFAULT_GOAL_MS), MIN_GOAL_MS, MAX_MS)))

    def _work_len(self, st=None):
        st = st if st is not None else self.state
        return max(MIN_WORK_MS, int(_clamp(st.get("work_ms", DEFAULT_WORK_MS), MIN_WORK_MS, MAX_PHASE_MS)))

    def _rest_len(self, st=None):
        st = st if st is not None else self.state
        return int(_clamp(st.get("rest_ms", DEFAULT_REST_MS), 0, MAX_PHASE_MS))

    # ---------- phase machine (wall-clock anchored) ----------
    def _advance(self, now):
        """Advance the work/rest cycle to where it IS in real time. Phase
        boundaries are anchored to the server wall clock, so a late client or a
        gateway restart cannot shift the rhythm — a phase expired at the moment
        it should. Transitions (v7): WORK end -> REST starts AUTOMATICALLY (the
        client chimes "time for a break"); REST end -> WAITING (the next work
        stretch waits for the user — the client rings the "break over" chime in
        a loop until resume/pause). Only runs while working/resting; WAITING
        and PAUSED sit still by design. Returns True if the state changed (the
        caller saves). CALLER MUST HOLD self.lock."""
        st = self.state
        if st.get("session") not in ("working", "resting"):
            return False
        changed = False
        for _ in range(_MAX_ADVANCE_STEPS):
            session = st.get("session")
            if session not in ("working", "resting"):
                break
            phase_end = float(st.get("phase_end_ms") or 0)
            if now < phase_end:
                break
            boundary = phase_end
            if session == "working":
                if float(st.get("accrued_ms", 0.0)) >= self._goal(st):
                    # the work stretch is over AND the goal is in -> SUCCESS
                    st["session"] = "success"
                    st["phase_end_ms"] = None
                    self._log_day(st["date"], self._goal(st), float(st.get("accrued_ms", 0.0)),
                                  float(st.get("rested_ms", 0.0)), int(st.get("cycles", 0)),
                                  done=True, failed=False)
                    changed = True
                    break
                # the work is done -> the break starts AUTOMATICALLY
                st["session"] = "resting"
                st["phase"] = "rest"
                st["cycles"] = int(st.get("cycles", 0)) + 1
                st["phase_end_ms"] = boundary + self._rest_len(st)
            else:  # resting -> waiting: the work stretch does NOT auto-start
                st["session"] = "waiting"
                st["phase"] = "work"
                st["phase_end_ms"] = None
                st["phase_left_ms"] = 0
            changed = True
        return changed

    # ---------- "block everything" window (mirrors workout reward window) ----------
    def _active_until_locked(self, now):
        """Epoch ms until which everything must be blocked (0 = do NOT block),
        computed from the current session state. Only a live (or paused
        mid-stretch) WORK window blocks; rests, waiting and a silenced waiting
        never hold the block. CALLER MUST HOLD self.lock (invoked from within
        status())."""
        st = self.state
        session = st.get("session", "idle")
        if session == "working":
            # work stretch in progress: the block ends AT the stretch's wall-clock
            # end, even if the client goes quiet (the phase machine auto-advances
            # on the next status/action).
            return int(st.get("phase_end_ms") or 0)
        if session == "resting" or session == "waiting":
            return 0
        if session == "paused":
            if st.get("wait_work"):
                # paused while WAITING between stretches -> nothing blocks
                return 0
            # paused inside a work stretch -> the block holds until you resume
            # (or the day ends); paused inside rest -> everything is already off.
            return self._midnight_after(now) if st.get("phase") == "work" else 0
        # idle / success -> nothing to block
        return 0

    # ---------- public api (called from app.py / secondary.py routes) ----------
    def status(self, now_ms=None):
        """Snapshot. Also (a) finalizes a new calendar day and (b) advances the
        phase machine to now, so the display and the resolver's block window
        stay correct even while the client is quiet. "awaiting_work" tells the
        UI it must be ringing the "break over" chime (waiting, or a silenced
        waiting)."""
        with self.lock:
            now = now_ms if now_ms is not None else self._now_ms()
            self._maybe_reset(now)
            if self._advance(now):
                self._save()
            st = self.state
            goal = self._goal(st)
            session = st.get("session", "idle")
            phase_end = float(st.get("phase_end_ms") or 0)
            phase = st.get("phase") if session in ("working", "resting", "waiting", "paused") else None
            if session == "paused":
                phase_left = int(st.get("phase_left_ms") or 0)
            elif session in ("working", "resting"):
                phase_left = int(max(0, phase_end - now))
            else:
                phase_left = 0
            accrued = float(st.get("accrued_ms", 0.0))
            return {
                "date": st["date"],
                "session": session,
                "phase": phase,                              # "work"|"rest"|None
                "phase_left_ms": phase_left,
                "awaiting_work": session == "waiting" or (session == "paused" and bool(st.get("wait_work"))),
                "goal_ms": goal,
                "work_interval_ms": self._work_len(st),
                "rest_interval_ms": self._rest_len(st),
                "accrued_ms": int(accrued),
                "remaining_goal_ms": int(max(0, goal - accrued)),
                "rested_ms": int(float(st.get("rested_ms", 0.0))),
                "cycles": int(st.get("cycles", 0)),
                "complete": session == "success",
                "cards": [dict(c) for c in st.get("cards", [])],
                "active_card_id": st.get("active_card_id"),
                # block-eom-thing window for the store to pick up (0 = off)
                "work_until_ms": self._active_until_locked(now),
                # stored work data (no display UI yet)
                "log": [dict(e) for e in st.get("log", [])],
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

    def set_active_card(self, cid):
        with self.lock:
            self._maybe_reset()
            self.state["active_card_id"] = int(cid) if cid else None
            self._save()
            return self.status()

    def set_goal(self, new_ms):
        """Set the DAILY GOAL (real work-time to reach in a day). Freely
        adjustable within [floor, cap]; the setpoint applies to today and to
        all future days (it survives the 00:00 reset). If it is lowered to the
        point that today's accrued work already covers it -> the goal is met
        (success)."""
        with self.lock:
            self._maybe_reset()
            st = self.state
            st["goal_ms"] = max(MIN_GOAL_MS, int(_clamp(new_ms, MIN_GOAL_MS, MAX_MS)))
            if st.get("session") in ("working", "resting", "paused") \
                    and float(st.get("accrued_ms", 0.0)) >= self._goal(st):
                st["session"] = "success"
                st["phase_end_ms"] = None
                self._log_day(st["date"], self._goal(st), float(st.get("accrued_ms", 0.0)),
                              float(st.get("rested_ms", 0.0)), int(st.get("cycles", 0)),
                              done=True, failed=False)
            self._save()
            return self.status()

    def set_interval(self, work_ms=None, rest_ms=None):
        """Set the pomodoro stretches. work_ms = length of a work stretch
        (block on, minutes count toward the goal); rest_ms = length of a rest
        stretch (block off, nothing counts). The stretch currently in progress
        keeps its remaining time; the new lengths apply from the NEXT stretch.
        rest may be 0 (straight to the waiting chime after the work stretch)."""
        with self.lock:
            self._maybe_reset()
            st = self.state
            if work_ms is not None:
                st["work_ms"] = max(MIN_WORK_MS, int(_clamp(work_ms, MIN_WORK_MS, MAX_PHASE_MS)))
            if rest_ms is not None:
                st["rest_ms"] = int(_clamp(rest_ms, 0, MAX_PHASE_MS))
            self._save()
            return self.status()

    def start(self):
        with self.lock:
            self._maybe_reset()
            st = self.state
            if st.get("session") not in ("idle",):
                return self.status()
            now = self._now_ms()
            st["session"] = "working"
            st["phase"] = "work"
            st["phase_end_ms"] = int(now + self._work_len(st))
            st["phase_left_ms"] = None
            st["wait_work"] = None
            st["session_start_ms"] = now
            st["last_tick_ms"] = now
            self._save()
            return self.status()

    def pause(self):
        """Freeze the running session, OR silence a ringing WAITING state. In
        both cases the block is released (a pause never holds the block on its
        own); a pause inside a work stretch keeps it only because the focus
        block must not leak — see _active_until_locked."""
        with self.lock:
            self._maybe_reset()
            st = self.state
            session = st.get("session")
            now = self._now_ms()
            if session == "waiting":
                # pause while waiting (between stretches): silence, nothing
                # blocks; a later resume starts the work stretch THEN
                st["session"] = "paused"
                st["phase"] = "work"
                st["phase_left_ms"] = 0
                st["wait_work"] = True
                st["last_tick_ms"] = now
                self._save()
            elif session in ("working", "resting"):
                st["phase_left_ms"] = max(0, int(float(st.get("phase_end_ms") or 0) - now))
                st["session"] = "paused"
                st["wait_work"] = None
                st["last_tick_ms"] = now
                self._save()
            return self.status()

    def resume(self):
        """Resume a pause (restoring the frozen stretch — or, for the next
        stretch, starting it fresh from the server clock), or start the next
        WORK stretch straight from a ringing WAITING state."""
        with self.lock:
            self._maybe_reset()
            st = self.state
            now = self._now_ms()
            if st.get("session") == "paused":
                if st.get("wait_work"):
                    # resumed a SILENCED WAITING (between stretches): start the
                    # next work stretch fresh from the server clock
                    st["session"] = "working"
                    st["phase"] = "work"
                    st["phase_end_ms"] = int(now + self._work_len(st))
                    st["phase_left_ms"] = None
                    st["wait_work"] = None
                    st["last_tick_ms"] = now
                else:
                    left = int(st.get("phase_left_ms") or 0)
                    if left <= 0:
                        # the frozen stretch expired while paused: start the NEXT
                        # stretch fresh from the server clock
                        st["phase_end_ms"] = int(now + (self._rest_len(st) if st.get("phase") == "work"
                                                        else self._work_len(st)))
                    else:
                        st["phase_end_ms"] = now + left
                    st["phase_left_ms"] = None
                    st["session"] = "resting" if st.get("phase") == "rest" else "working"
                    st["wait_work"] = None
                    st["last_tick_ms"] = now
            elif st.get("session") == "waiting":
                # the break is over and the user chose to work NOW
                st["session"] = "working"
                st["phase"] = "work"
                st["phase_end_ms"] = int(now + self._work_len(st))
                st["phase_left_ms"] = None
                st["wait_work"] = None
                st["last_tick_ms"] = now
            self._save()
            if self._advance(now):
                self._save()
            return self.status()

    def tick(self, delta_ms):
        """Credit real time for the current phase (capped by REAL elapsed
        wall-clock since the last tick): a work stretch credits the GOAL (and
        the focused card), a rest stretch credits the rest-time. Also advances
        the phase machine to now. Waiting/paused/idle sessions credit nothing
        (they are not running). Returns (snapshot, error_string_or_None)."""
        now = self._now_ms()
        with self.lock:
            self._maybe_reset(now)
            st = self.state
            session = st.get("session")
            if session == "success":
                return self.status(), None
            if session not in ("working", "resting"):
                return self.status(), "work not running (start it, or resume the waiting chime)"
            try:
                delta_ms = float(delta_ms)
            except (TypeError, ValueError):
                return self.status(), "bad delta_ms"
            delta_ms = max(0.0, min(delta_ms, _MAX_DELTA_MS))
            last = float(st.get("last_tick_ms") or now)
            # Cheat-resistance: credit capped by REAL elapsed wall-clock —
            # a hostile client can't fast-forward either the goal or the rest.
            credit = min(delta_ms, max(0.0, now - last))
            st["last_tick_ms"] = now

            if session == "working":
                st["accrued_ms"] = min(float(self._goal(st)),
                                       float(st.get("accrued_ms", 0.0)) + credit)
                # attribute the delta to the focused card (fall back to the first one)
                cards = st.get("cards", [])
                target = next((c for c in cards if c.get("id") == st.get("active_card_id")), None)
                if target is None and cards:
                    target = cards[0]
                if target is not None:
                    target["accrued_ms"] = float(target.get("accrued_ms", 0.0)) + credit
            else:  # resting — the break is recorded but never credited to the goal
                st["rested_ms"] = float(st.get("rested_ms", 0.0)) + credit

            if session == "working" and float(st.get("accrued_ms", 0.0)) >= self._goal(st):
                # reached the goal mid-stretch -> SUCCESS (block cleared by caller)
                st["session"] = "success"
                st["phase_end_ms"] = None
                self._log_day(st["date"], self._goal(st), float(st.get("accrued_ms", 0.0)),
                              float(st.get("rested_ms", 0.0)), int(st.get("cycles", 0)),
                              done=True, failed=False)
                self._save()
                return self.status(), None
            self._advance(now)
            self._save()
            return self.status(), None
