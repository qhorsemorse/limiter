# HANDOFF PROMPT — "Control Limits" (v7 — central + secondary architecture + Work pomodoro goal with chimes, ~2026-11-14)

=== BEGIN HANDOFF PROMPT ===

## 0d. WHAT CHANGED THIS SESSION (v6 → v7): WORK TIMER — SELF-LOCK REMOVED + CHIMES + NO AUTO-RESTART

The Work timer (`work.py` / `work.html` / `test_work.py`, wired in `app.py` +
`secondary.py`) had two user-requested changes. The v6 state machine and all
its cheat-resistance/anchoring are otherwise UNCHANGED — read the `work.py`
module doc (v7) for the full rules.

1. **REMOVED: the "day ends short of goal" self-lock (the bug).** In v6, when
   00:00 caught the session active with the goal unmet, the session became
   `failed`, a penalty window (`penalty_ms`) blocked EVERYTHING, and no new
   session could start until the next 00:00 — i.e. the timer locked *itself*.
   The user said that was a misunderstanding: blocking (at most) the app
   time-limits was the intent, and he doesn't even want that — so the whole
   mechanism is GONE (v7): at 00:00 the day is only LOGGED as failed in the
   per-day work-data log, the new day starts **IDLE**, and the timer is
   always startable. Concretely: no `failed` session exists anymore, no
   `penalty_until_ms` / `penalty_ms` fields, no `set_penalty` API action (the
   route was deleted from `app.py` AND `secondary.py`), no `penalty_*` keys in
   `GET /api/work` (also dropped: the `failed` status key). The daily `log`
   still carries a `failed` boolean — stored work data only.
   **Migration:** `_load` heals a legacy state file stuck in the old
   `session == "failed"` state to `idle` (remembering it was active-via
   `day_ended_active` so the day still logs as failed) and pops the legacy
   penalty fields. The block window (`store.work_until`) is now ONLY: the
   exact end of a live work stretch, or until-resume/00:00 when paused
   *mid work stretch*; rests, waiting and a silenced-waiting pause are 0.
2. **NEW RHYTHM + CHIMES (v7):** the break no longer auto-chains into the next
   work stretch. `resting` now ends in a new **`waiting`** state: block off,
   no auto-work, `session=="waiting"`, `phase=="work"`, `phase_left_ms==0`,
   and the status exposes **`awaiting_work: true`** (also true for a pause done
   from waiting, via the internal `wait_work` marker). `resume` from waiting
   (or from that silence-pause) starts a FRESH work stretch anchored to the
   server clock; `pause` while waiting silences it (paused, `awaiting_work`
   true, block 0). The chimes live ENTIRELY in the client
   (`work.html`, WebAudio tones — no audio files, stdlib-friendly):
   * work done (`working -> resting`, and also `working -> success`) → the
     page plays the "time for a break" chime (two-note A5/E6 rise);
   * break over (`resting -> waiting`) → the page plays the "break over" chime
     (E5-G5-B5 call) and then LOOPS it every 10 s until the session leaves
     waiting (resume or pause); the dial shows `--:--` with a pulsing amber
     "BREAK OVER · RINGING" box; the UI offers "Start work stretch" (resume)
     and "Pause (silence)".
   * The server never plays audio — it only flips states; the page polls
     (≤1 Hz flushes while active, 5 s loads otherwise) and fires chimes on
     state transitions, so the tab must be open (and sound unlocked on the
     first click — the 🔊 "test chime" button exists for that). No chime if the
     tab was closed when a transition flipped server-side. Acceptable by design
     (single-user device).
3. **API diff (both gates):** `POST /api/work` loses `set_penalty`; `GET
   /api/work` loses `penalty_ms / penalty_active / penalty_left_ms / failed`
   and gains `awaiting_work`; `session` may now be `"waiting"`.
   Everything else (goal/intervals/cards/tick semantics, credit capping,
   phase anchoring, boot restore of the work block window) is unchanged.
   Env var `WORK_PALTY_MS` is DEAD (removed).
4. **Tests:** `test_work.py` rewritten around the v7 flow (unit: waiting
   state, resume-from-waiting, pause-while-waiting silence, day-end → fresh
   idle start + failed log entry; integration: blocked during work, free
   during rest, still free while WAITING, re-blocked after resume, success
   stays free, legacy stuck `failed` state heals over HTTP with no penalty
   block). **All 9 suites green** (ran live with the secondary gateway up —
   the suites use :8199/:5358/:5354/:8081/:8082/:8083/:8444/:8450, so the
   running :8080/:8443 do not collide).
5. Docs updated: README §1/§2/file-map/env/API, this file.

## 0c. WHAT CHANGED (v5 → v6): WORK TIMER IS NOW A POMODORO GOAL TIMER

The **Work** feature (`work.py` / `work.html` / `test_work.py`, wired in `app.py` +
`secondary.py`) was RECONCEPTUALIZED at the user's request. The old design (a
button-timer with a *max budget* against a *monotonic common minimum*) is GONE.
New concept — read the `work.py` module doc for the full rules:

* **Daily goal**: the user sets a target of real work-minutes per day
  (`POST /api/work {action:"set_goal", ms}` or the UI). Settable in BOTH directions
  (clamped to `[MIN_WORK_GOAL_MS, 8h]`), stored in `data/work.json`, survives the
  00:00 reset. The old `limit_ms`/`budget_ms` no longer exist (`set_limit` /
  `set_budget` / `budget_ms` are removed; old files migrate: `limit_ms` → `goal_ms`).
* **Pomodoro stretches**: work stretch (default 50 min) / rest stretch (default
  10 min), settable via `{action:"set_interval", work_ms?, rest_ms?}` (rest may be
  0). The timer cycles WORK → REST → WORK until the goal is met.
* **Blocking follows the phase**: WORK stretch → the resolver blocks EVERYTHING
  (via the same `store.set_work_until`/`work_active` window as before); REST
  stretch → the block is OFF (a real break). `work_until_ms` is now the exact
  wall-clock end of the current work stretch (no more worst-case whole-session
  window). Paused-during-work keeps blocking until resume/00:00;
  paused-during-rest stays unblocked.
* **Only work minutes count**: real work-stretch time is credited to the goal
  (`accrued_ms`); rest time is only *recorded* (`rested_ms`). A day reaching the
  goal → `success` (done for the day). A day that ENDS at 00:00 with the session
  still active and the goal unmet → `failed`: the penalty window arms
  (`WORK_PALTY_MS`, default 30 min), the day is logged as failed, no new session
  until 00:00. (There is no more mid-session "budget ran out" failure.)
* **Work data is stored (no display UI yet, per the user)**: a per-day record in
  `data/work.json` → `"log"`: `{date, goal_ms, worked_ms, rested_ms, cycles, done,
  failed}`. It is included in `GET /api/work` so a future progress UI can display
  it. The user explicitly said the progress-display UI is NOT now.
* **Cheat-resistance kept + strengthened**: credit (work AND rest) is capped by
  real elapsed wall-clock between ticks; phase boundaries are anchored to SERVER
  time (a phase ends exactly when it should even if the client lags/restarts),
  and `status()` auto-advances expired phases. Because of that auto-advance,
  `app.py` and `secondary.py` now re-sync the store's block window on
  `GET /api/work` as well (after every POST it already did).
* All 9 suites are green (`test_work.py` rewritten: unit + live end-to-end proving
  blocked-during-work / unblocked-during-rest / re-block / success / day-end
  penalty through the real resolver). `test_app.py` / `test_limit.py` now boot
  their test app on web :8199 with `--no-doh --no-bypass` so they can run while a
  live gateway holds :8080/:8443 (same reason `test_work.py` uses :8199).

You are taking over a project called **Control Limits** at `/home/pietro/limiter/`.
This is a REAL, running project. Read this document, then read the code, then do
the "NEXT STEPS" in order. Do NOT re-investigate things documented here — they
were already decided and verified.

The user is non-technical. Deliver working software; explain conclusions in plain
language. You may work alongside another AI in parallel on the same tree — keep
your changes in the modules you own and re-run the test suite before declaring done.

---

## 0. WHAT CHANGED THIS SESSION (prior state → now)

The previous handoff (v4) described a **single-host** gateway: one machine held
all limits in local SQLite, ran the DoH front-door, the plain-DNS sinkhole, the
bypass watcher, and the web UI — everything on one box.

This session re-architected it into a **central / secondary** design so limits can
be managed from *one* place and enforced on *many* devices:

- **Central server** — ONE machine holds all limits (the single source of truth)
  plus the admin API/UI. It just sits in the background ("serving on the side");
  nobody browses through it. Run it with the new `--no-sinkhole --no-doh
  --no-bypass` flags so it doesn't waste resources binding DNS/DoH it never uses.
- **Secondary server** (`secondary.py`) — runs on every *other* device. Its **sole
  purpose is to fetch all limits from the central and enforce them on that device.**
  It runs the device's own DoH front-door (the device's Firefox points at it) and
  its own device-local bypass watcher, but resolves every limit against a **local
  cache that is a copy of the central's limits DB**, syncing from the central as
  needed. Auto-starts at boot, runs while the device is used (`tools/install_secondary.sh`).
- **`remote_store.py`** — the `RemoteStore` class: a drop-in reader/evaluator for
  the resolution core (`dns.resolve_bytes` / `doh`) backed by the local cache.
  Implements the cache-first / staleness (TTL) / offline (emergency) semantics.

The original single-host mode still works untouched (`app.py` with no new flags) —
nothing regressed: **all 8 test suites are green** (the new one is `test_work.py`).

---

## 1. WHAT THIS IS

A parental-domain blocker. Whole domains are blocked by intercepting **DNS**: a
blocked domain answers `0.0.0.0` (fails instantly); an allowed domain is forwarded
to the real upstream resolver. There is a small web UI to manage limits. Anything
that tries to *bypass* the blocker (VPN up, Firefox using a public DoH provider, or
the lock file being edited) arms a **NULEAR** fallback that blocks *every* domain
(except localhost) until the cause goes away or a parent clicks "Override".

Everything runs locally. No cloud, no accounts, no phone. **Stdlib-only Python — no
pip.** `systemd --user` is allowed (no root needed).

## 2. HARD REQUIREMENTS (non-negotiable)

1. Blocking MUST work inside real Firefox (the whole point) — via the DoH front-door
   on loopback (`network.trr.mode=2`, 100% of DNS routed through us).
2. The secondary's DoH front-door enforces the **central's** limits; the central
   remains the single source of truth.
3. Bypass (VPN / public-DoH / lock tamper) → nuclear mode, auto, root-free detection
   (device-local, unchanged).
4. DoH/DNS lock must be *hard for the child to reset* (file/policy based, no GUI).
5. **No sudo, no pip, stdlib only.** `systemd --user` allowed.
6. A lock is ALWAYS enforced (see §3c offline/emergency). A locked domain can never
   be bypassed while offline.

## 3. ARCHITECTURE — how a request flows

### 3a. CENTRAL (only limits live here)
```
admin (UI + API)  ──▶  app.py  :8080   GET/POST /api/limits, POST /api/detect
                        (central-only mode: --no-sinkhole --no-doh --no-bypass)
                        * ticker time-limits centrally (state machine lives here)
                        * no sinkhole, no DoH front-door, no bypass watcher needed
```

### 3b. SECONDARY (per controlled device)
```
Firefox (DoH, locked to 127.0.0.1:<dohport> on THIS device)
        │ every DNS query, TLS, loopback
        ▼
  secondary.py  ──┬─ doh.py  :8443  the device's front-door (enforcement channel)
                 ├─ dns.resolve_bytes  (shared core, now fed by RemoteStore)
                 ├─ bypass watcher    (device-local nuclear detection)
                 ├─ sync ticker       (keeps the local cache fresh, TTL-driven)
                 ├─ workout (per-device, local)
                 ├─ work          (per-device, local) — pomodoro goal timer; block-e window follows work stretches, honours store.work_active(now)
                 └─ child web UI/API  :8080   (status + READ-ONLY limits view + work nav)
                     │
                     ▼  RemoteStore — answers from a LOCAL COPY of the central DB
                     │   1. check local cache
                     │   2. if absent/stakeness (older than row TTL) → GET /api/limits from central, refresh cache
                     │   3. evaluate from the fresh cache
                     │   4. on an actual allowed A-lookup → POST /api/detect to central (arms grace centrally)
```

### 3c. CACHE + OFFLINE / EMERGENCY (the important part)
The local cache is a **copy of the central `limits` table** (`remote_store.CacheDB`),
each row carrying `updated_at` (mirrored from the central's `updated_at`, which the
central stamps via `db.save_state` every time a limit's state machine changes). This is
deliberate **NOT** a per-key TTL cache — it's "cache = DB copy, refreshed on request."

For **every** request (locked / locked-with-time-limit / free) the flow is:
  - look in the local cache first; if the row is absent or its `updated_at` is older
    than the TTL (default 30 s), do `GET /api/limits` on the central, replace the cache,
    stamp the sync time, then answer from the fresh cache. (Stakeness check = row age.)
  - locked (absolute, or a time-limit currently in cooldown) → **blocked**.
  - free / unlimited → forward to the real upstream.

While the device is **out of the central's LAN**:
  - the secondary still attempts to reach the central every TTL (so it notices when it
    comes back); a failed attempt flips **emergency** mode.
  - **Locked** domains are **always blocked** (determinable from the last cache snapshot —
    no central call needed). You can't reach them until you're back on the LAN.
  - **Free / unlimited** domains: this is where the original spec was slightly ambiguous
    ("no-lock sites go through" vs "block everything, free included, until a central sync
    succeeds"). We default to the **safe, fail-closed** behaviour: while offline we cannot
    confirm a free site is genuinely unrestricted, so it is **blocked** too, and everything
    resumes instantly on reconnect. Flip `secondary.py --allow-free-offline` (or set
    `allow_free_offline=True` on `RemoteStore`) for the alternative (forward free sites even
    while offline). **Locks are always blocked either way.**

## 4. FILE MAP (`/home/pietro/limiter/`)

Files **unchanged** from v4 (still the single-host engine): `engine.py`, `db.py`
(`updated_at` column added for TTL/staleness), `bypass.py`, `tools/`.

Files **changed** from v4 to add the **Work / focus-session** feature (a *sibling*
of the workout with the opposite effect):

| File | Change |
|---|---|
| `dns.py` | `resolve_bytes` gains a block-e window: while `store.work_active(now)` is true, every domain (time-limit AND absolute) resolves to `0.0.0.0` (localhost exempt) — the inverse of the workout reward pass. Placed right after the reward check and before nuclear. |
| `store.py` / `remote_store.py` | Added `work_until`, `set_work_until(ms)`, `work_active(now)` — the block-e window, synced from the Work state machine after each `GET` and mutating `POST /api/work` (v6: `GET` too, because `status()` auto-advances phases) and restored on boot. |
| `app.py` | Serves `/work.html`; `GET/POST /api/work` (add/remove/set_card/start/pause/resume/tick); restores the work block-e window on boot via `set_work_until`. |
| `secondary.py` | Serves `/work.html`; `GET/POST /api/work`; per-device Work state machine; syncs `set_work_until` onto the `RemoteStore` so the block-e is enforced on the device. |
| `index.html` / `workout.html` / `secondary_index.html` | Nav bar gains a **Work** link. |

**NEW this session (work feature):**

| File | Role | Status |
|---|---|---|
| `work.py` | Daily **pomodoro goal** state machine (v7): settable daily goal + work/rest stretch lengths, wall-clock-credited work-time (rest only recorded), block only during work stretches, auto-rest after a work stretch, the waiting chime state (break over → waits for resume/pause), day-end short-goal logged only (NO failure/penalty self-lock), per-day work-data log. Persists to `data/work.json`. | done, tested |
| `work.html` | Page 3 — goal + stretch settings, the work/rest dial, start/pause/resume, task cards, WebAudio **chimes** (break-start chime, break-over chime looping every 10 s, 🔊 test-chime button), "worked / goal" line (no fancy progress UI yet by design). | done |
| `test_work.py` | Work feature tests: unit for the state machine + live end-to-end through the real DNS resolver (blocks every domain during work, un-blocks during rest, stays un-blocked while WAITING, re-blocks on resume, un-blocks on completion, legacy "failed" state heals). | green |

**this handoff's prior NEW table (central/secondary, v5) is still valid and unchanged.**

**NEW this session:**

| File | Role | Status |
|---|---|---|
| `remote_store.py` | `CacheDB` (local copy of central `limits`) + `_http_json` helper + `RemoteStore` (cache-first / staleness / offline semantics; drop-in for `store.Store` in the resolution core). | done, tested |
| `secondary.py` | The secondary gateway: DoH front-door (`doh.serve` with a `RemoteStore`), device-local bypass watcher, sync ticker, child web UI (`secondary_index.html`) + status/stats/workout endpoints. Auto-starts at boot. | done, tested |
| `run_central.py` | Thin launcher → `app.py` in central-only mode (wraps `--no-sinkhole --no-doh --no-bypass`). | done, tested |
| `run_secondary.py` | Thin launcher → `secondary.py` (symmetric role entrypoint). | done, tested |
| `test_remote_store.py` | Unit/integration tests for `RemoteStore` against a live central-only `app.py`: sync, cache-copy, staleness re-fetch, `report_access`, offline emergency, fail-closed vs `allow_free_offline`. | green |
| `test_secondary.py` | End-to-end test: central(central-only) + stub upstream + secondary; drives a real DoH query through the secondary's front-door → blocked/forwarded correctly, plus detect arms grace centrally. | green |
| `tools/limiter-central.service` | systemd --user unit for the central (placeholder `__BASE__`, runs `run_central.py`). | done |
| `tools/limiter-secondary.service` | systemd --user unit for the secondary (placeholders `__BASE__` / `__CENTRAL__`, runs `run_secondary.py`). | done |
| `tools/install_central.sh` | Instals the central on a machine: installs+enables+starts `limiter-central.service`. | done |
| `tools/stop.sh` | Stops any running Control Limits process (sweeps `app.py`/`run_central.py`/`run_secondary.py`). | done |
| `tools/install_secondary.sh` | Installs the secondary on a device: writes `data/secondary.json`, ensures certs, installs+enables+starts the systemd unit. | done |
| `tools/stop_secondary.sh` | Stops/disables the secondary unit. | done |

## 5. RUN IT

**Central server** (one machine, sits on the side — no browsing through it):
```bash
python3 run_central.py --web 8080 --db data/limits.db   # == app.py --no-sinkhole --no-doh --no-bypass ...
# then add limits in the UI at http://<central-ip>:8080
```

**Secondary server** (every other device):
```bash
bash tools/install_secondary.sh http://<central-ip>:8080
#    == unit ExecStart: python3 run_secondary.py --central http://<central-ip>:8080 --web 8080 --dohport 8443 --certdir <base>/certs --uphost 127.0.0.53 --ttl 30
# writes data/secondary.json, ensures certs, systemctl --user enable --now limiter-secondary.service
systemctl --user status limiter-secondary.service
journalctl --user -u limiter-secondary.service -f
```
The device's Firefox is pointed at the secondary's DoH front-door with the existing
`tools/setup_firefox.sh` (writes the TRRONLY lock + imports the CA into that device's
profile), then a full Firefox restart. From then on, the central's limits are enforced
on that device.

**Manual DoH probe** through a secondary's front-door:
```bash
python3 -c "import sys; sys.path.insert(0,'/home/pietro/limiter'); from doh import build_wire_query; open('/tmp/q.bin','wb').write(build_wire_query('prom.ua'))"
curl -s --cacert /home/pietro/limiter/certs/ca.pem -H 'Content-Type: application/dns-message' --data-binary @/tmp/q.bin https://127.0.0.1:8443/dns-query | od -A d -t x1z
# blocked → last bytes 00 04 00 00 00 00 ; allowed → real upstream IPs
```

## 6. TEST IT — IMPORTANT CAUSE

All 8 suites are plain-Python (no pytest), self-contained (throwaway sqlite in /tmp,
fake upstreams). `test_dns` and `test_app` (and, for the work feature, `test_work`)
bind their stub upstream on **:5354**, which the *live* gateway also binds — run them
with the gateway stopped:
```bash
systemctl --user stop limiter                 # free :5354 (and :8080) for the stubs
for t in test_engine test_dns test_doh test_app test_bypass test_remote_store test_secondary test_work; do python3 $t.py; done
systemctl --user start limiter                # (or start it however you like)
```
Failure signature NOT to mistake for a bug: `OSError: [Errno 98] Address already in use`
on `127.0.0.1:5354` means the gateway is still running. `test_bypass.py` still does a
*live* public-DoH socket scan (doubles as the security alarm).

## 7. LIVE STATE (verify before assuming stale)
- Single-host `limiter.service` **active** on :8443/:5354/:8080 (the old mode still works;
  not part of the central/secondary flow unless you want one box to also browse).
- `certs/` present (shared by both central and secondaries).
- All 8 test suites green (incl. the new `test_work.py`).

## 8. GOTCHAS (do-not-redo)
- **Cache-first, not per-key TTL.** The `RemoteStore` cache is a *copy* of the central DB,
  refreshed when the newest row's `updated_at` is older than the TTL. Do NOT convert it into
  a per-domain TTL cache.
- **`updated_at` is the staleness key.** The central stamps it on every state change
  (`db.save_state`); secondaries key their TTL off it.
- **Central-only mode needs the 3 new flags** (`--no-sinkhole --no-doh --no-bypass`); the
  DoH front-door / sinkhole / watcher only make sense on a browsing device (secondary).
- **`report_access` / `/api/detect` arms the grace timer centrally** (once), so time-limit
  timers stay consistent across every secondary device. A secondary must POST to `/api/detect`
  for every actually-reached domain (done inside `RemoteStore.detect`, called from
  `dns.resolve_bytes` on allowed A-queries).
- Snap Firefox profile lives under `~/snap/firefox/common/…`. Snap has a private /tmp.
- `_proc/net/*` hex addresses are **host-byte-order u32** on little-endian — reverse the bytes.
- `doh.py` uses `ThreadingHTTPServer` + `ctx.wrap_socket(server_sock)`.
- **Work block-e window (`store.work_active(now)`)** sits in `dns.resolve_bytes` RIGHT AFTER the reward check and BEFORE nuclear — it is the inverse of the workout reward: while active, EVERY domain (time-limit AND absolute) resolves to `0.0.0.0` (localhost exempt). v7: the window equals the exact wall-clock END OF THE CURRENT WORK STRETCH (0 during rests AND during the waiting chime state, until-resume/00:00 when paused mid work stretch, 0 for a silenced waiting pause; the old penalty-expiry case is gone). Set/clear it via `set_work_until(ms)` — synced after every `GET` AND `POST /api/work`, and re-armed on boot. It is a per-device LOCAL concern on the secondary (synced onto the `RemoteStore`), like the workout — the central does not own it.
- **Work (v6) cheat-resistance & rhythm**: credited work/rest time is capped by real elapsed wall-clock between ticks (never trust a big client-reported delta), and the work/rest phase boundaries are anchored to the SERVER wall clock (a phase ends exactly when it should, even across client lag / gateway restart; `status()` auto-advances expired phases and `Work._advance` is the ONLY place phases flip). The daily GOAL is freely settable (NOT monotonic — that was the old minimum) but clamped to `[MIN_WORK_GOAL_MS, 8h]`.
- **Work (v7) a missed goal NEVER locks anything**: 00:00 catching an active session short of the goal only LOGS that day as failed (in `data/work.json["log"]`) and starts a fresh IDLE day — no `failed` session, no penalty window, `start` always works (v6's self-lock was a user-reported misunderstanding and is fully removed). The per-day log record is THE stored work data the user asked for (display UI explicitly deferred).
- **Work (v7) chimes are CLIENT-side** (`work.html`, WebAudio): the server only flips states at the chime points (`working->resting` / `working->success` = "time for a break", `resting->waiting` = "break over" + loop every 10 s until resume/pause). The `waiting` state is the server-side half; `awaiting_work` in the status drives the UI. Don't move chime synthesis server-side; don't add a new auto-chaining transition in `_advance` (waiting must wait for the user).
- `index.html`/`secondary_index.html` are served stale until the browser refreshes.
- Fail-closed offline is the default; `--allow-free-offline` flips it. Locks block always.

=== END HANDOFF PROMPT ===

## NEXT STEPS (in priority order)
0. **Per-limit, one-directional (add-only) domain management — DONE (this session).
   Clicking a limit in the UI opens a dedicated detail page at `/limit/<id>`
   (`limit.html`) where you can see the limit's type / conditions / current domains
   and ADD new ones. The mutation path is one-directional: `POST /api/limits/<id>/domains`
   (via `db.append_domains_to_limit` + `Store.add_domains`) only appends + dedups — there
   is intentionally NO delete/remove-domain endpoint anywhere, so undoing an add is only
   possible by hand-editing the hidden `data/limits.db` (WAL-mode, outside the web root),
   matching the project's "difficult to undo" philosophy. Backend + UI wired into
   `app.py` (GET /api/limits/<id>, POST /api/limits/<id>/domains, /limit/<id> route),
   `db.py`, `store.py`, and `index.html` (limit cards are now clickable → detail page);
   new file `limit.html`. Tested by `test_limit.py`. **All 9 suites green.**
1. **Work / pomodoro-goal feature — v7 (this session; v6 reconcept before it).** `work.py` + `work.html` + `test_work.py` rewritten twice (see §0d for v7 — the current rules/API — and §0c for the v6 history). All 9 suites green. There is now a settable DAILY GOAL, 50/10-style work/rest pomodoro stretches (block on during work, off during rest), only work minutes credited, a per-day stored work-data log, and chimes: rest auto-starts when a work stretch is done, and a break ends in the WAITING state whose chime loops until the user resumes (fresh work stretch) or pauses (silence). A day ending (00:00) short of the goal is logged only — the timer is never locked (the v6 penalty self-lock is REMOVED; do not resurrect it). If the user asks for the progress display next, the data is already in `GET /api/work` → `log`.
2. **Live end-to-end proof on real hardware.** Everything is proven in-process
   (test_secondary drives real DoH wire queries through the secondary). The one thing
   left is a real Firefox on a second device pointed at a secondary (steps in §5). If a
   blocked domain still loads, the usual causes: certs missing (DoH off), lock not
   applied on THAT device (Firefox not routing through us → watcher may go nuclear,
   which is still blocking), or upstream forward broken.
3. **`install_policy.sh` on the user side** (sudo) to lock Firefox's DoH settings
   read-only on each secondary, so the child can't switch to Cloudflare/Google.
4. **Optional — whole-OS plain-DNS control** on secondaries (`bypass.py` already
   generates the ruleset; needs root). Treat as dormant.
5. **Optional polish / refactors** the user is currently reviewing by hand — nothing
   blocks shipping.
