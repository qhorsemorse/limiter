# Control Limits

A parental/self-control **DNS blocking gateway** built on a **central / secondary**
topology for one Linux box (a Raspberry Pi on a home LAN):

- The **central server** holds *all* the limits (the single source of truth) and the
  admin UI/API. It just sits on the side — nobody browses through it.
- Every **secondary device** runs `secondary.py`. Its *sole* job is to fetch the
  central's limits (into a local cache) and enforce them on that device through its
  own DoH front-door. The device's Firefox points at the secondary (not the central),
  so the central's rules are what block sites here.

One Firefox install is the controlled browser: its DNS is forced through the local
secondary (DNS-over-HTTPS on loopback), and the secondary blocks or delays domains
based on the central's rules. Anything that tries to *escape* (VPN up, public DoH,
disabling the lock) triggers a "nuclear" fallback that blocks *everything* until the
parent acknowledges.

Everything runs locally. No cloud, no accounts, no phone.

---

## 1. How it works (60-second version)

```
Firefox (DoH, locked to 127.0.0.1:8443)
        │  every DNS query, TLS, loopback
        ▼
  app.py  ──┬─ doh.py      :8443  (the browser front-door)
            ├─ dns.py      :5354  (plain-DNS sinkhole, UDP+TCP: legacy/tests/future whole-OS)
            ├─ engine.py   (limit rules + timers, pure logic)
            ├─ store.py    (thread-safe state bridge, sqlite persistence)
            ├─ bypass.py   (watcher: VPN / public DoH / lock tamper → NUCLEAR)
            ├─ workout.py  (daily hover-timer → √x·ln(x)·0.9 no-block reward)
            ├─ work.py     (daily button-timer focus session → blocks everything + penalty)
            └─ web UI + API :8080 (http://PI-IP:8080)
```

* **Blocked domain** → answered `0.0.0.0` (fails instantly). **Allowed domain** →
  forwarded to the real upstream resolver (`127.0.0.53`, systemd-resolved).
* **Limits** (set in the web UI, stored in `data/limits.db`):
  * `absolute` — always blocked (e.g. a game/anime site).
  * `time` — "grace then block": when a listed domain is first reached, the child
    gets `grace` minutes of freedom, then the domain is blocked for `cooldown`
    minutes, then free again. (First detection starts the clock.)
* **Bypass watcher** (every 5 s): if it sees a VPN up, Firefox talking to public
  DoH endpoints, Firefox resolving via the OS resolver (DoH turned off), or the
  profile lock file `user.js` deleted/edited → **NUCLEAR**: every domain resolves
  to 0.0.0.0 (except localhost) until the cause is gone **or** the parent clicks
  "Override" in the UI (the red banner).
* **DoH provider lock** (Firefox enterprise policy): the DNS-over-HTTPS section
  in Firefox settings is read-only and pinned to our gateway — the child cannot
  switch to Cloudflare/Google/custom in the UI.
* **Browser lock** (optional, hourly, automated): every browser other than
  Firefox on the machine is disabled (snap `disable` / binaries `chmod 000`),
  so "open Chrome instead" is not an escape route.
* **Workout + reward** (daily): while a cursor hovers the big timer it counts
  down (leave = pause). Minimum duration is **25 minutes**; before the first
  hover of the day you can pick a longer one in the UI (30/40/45/60/90/120/
  180 min) — once you hover, the choice locks until 00:00. The timer must
  reach **zero** to earn anything (a partial session earns nothing). The
  reward length is a hardcoded ratio of the workout length (x = minutes):
  **y = √x · ln(x) · 0.9 minutes** (25 → ~14.5 min, 60 → ~28.5 min, 120 →
  ~47 min). Redeeming (button) suspends **all** blocking (limits AND nuclear)
  for that duration, then reverts automatically. The workout is once per
  calendar day (renews at local 00:00); the reward is same-day-only and
  non-stackable.

* **Work / focus session** (daily — a *sibling* of the workout but the opposite
  intent). While a session is active (or a penalty is pending) the gateway blocks
  **everything** — both time-limits and absolute limits, no matter how much
  freedom a domain usually has — so there is nowhere to get distracted while you
  work. It is a **button** timer (start / pause / resume), not hover: the
  countdown freezes on pause and resumes on resume. You set a common **minimum** of
  real work-time (shared by every *work card* — you can add several tasks of
  different kinds) and a max **timer**. Reach the minimum before the timer runs
  out → done, block clears; run the timer out first → a **penalty** window arms
  (everything stays blocked, then unlocks). The minimum is locked when the first
  card is created and can only ever grow. State and the penalty window survive
  restarts (`data/work.json`); the block window is re-armed on boot. Cheat
  resistance matches the workout: work-time is capped by the real elapsed
  wall-clock between ticks, so it cannot be fast-forwarded.

---

## 1b. Central / Secondary architecture

The single box now plays two possible roles:

**Central server** — one machine that holds *all* limits and the admin UI/API.
Run it so it never tries to be a DNS front-door:

```bash
python3 app.py --no-sinkhole --no-doh --no-bypass --web 8080 --db data/limits.db
```

It serves `GET/POST /api/limits` and `POST /api/detect`. The `--no-*` flags drop
the plain-DNS sinkhole, the DoH front-door, and the device-local bypass watcher
(these only make sense on a *browsing* device). Time-limit timers live here too,
so the state machine is consistent across every secondary. Nobody browses through
the central.

**Secondary device** (`secondary.py`) — runs on every other machine. Its SOLE job
is to fetch the central's limits into a local cache and enforce them here:

```bash
bash tools/install_secondary.sh http://<central-ip>:8080
```

It runs:
* the device's **DoH front-door** (:8443) — the device's Firefox points at THIS
  machine, so the central's limits are what block sites here;
* a **device-local bypass watcher** (VPN / public-DoH / lock tamper → nuclear);
* a **sync ticker** that keeps the local cache fresh (TTL per row = the row's
  `updated_at` age); and
* a **child web UI** (:8080) showing nuclear/offline status, a READ-ONLY view of
  the central's limits, stats, and the per-device workout timer.

It auto-starts at boot (`systemd --user limiter-secondary.service`) and runs while
the device is used.

**The local cache (not a normal per-key cache).** Each secondary keeps a copy of
the central `limits` table (`remote_store.CacheDB`), each row stamped with
`updated_at` (the central stamps it every time a limit changes). For **every**
query the secondary: checks the cache → if absent or older than the TTL (default
30 s) does `GET /api/limits` on the central and refreshes → answers from the fresh
copy. Locked domains are always blocked. While offline the secondary keeps trying
ever TTL; a failed contact sets **emergency** mode and every domain is blocked
(fail-closed) until it can reach the central again — reconnect restores everything
instantly. Flip `--allow-free-offline` to instead forward unrestricted sites while
offline. Locks are always blocked either way.

Run the secondary's own DoH probe (blocked → last bytes `00 04 00 00 00 00`):
```bash
python3 -c "import sys; sys.path.insert(0,'/home/pietro/limiter'); from doh import build_wire_query; open('/tmp/q.bin','wb').write(build_wire_query('prom.ua'))"
curl -s --cacert /home/pietro/limiter/certs/ca.pem -H 'Content-Type: application/dns-message' --data-binary @/tmp/q.bin https://127.0.0.1:8443/dns-query | od -A d -t x1z
```

## 2. Files

| Path | What it is |
|---|---|
| `app.py` | Main gateway: DNS+DoH servers, web UI/API, bypass watcher, hourly browser re-lock, workout reward restore |
| `engine.py` | Pure limit logic (detect/tick/evaluate, domain matching incl. subdomains) |
| `store.py` | Thread-safe state (limits, nuclear, override, reward window, stats) + sqlite persistence |
| `dns.py` | Plain-DNS sinkhole (UDP/TCP) + the shared resolution core `resolve_bytes` used by BOTH channels |
| `doh.py` | TLS DNS-over-HTTPS responder (RFC 8484, wire + JSON) — the browser front-door |
| `bypass.py` | Bypass detection (VPN peers, public-DoH sockets, Firefox process/sockets, lock-file check) + optional iptables rules (used only with `--bypass-protect`) |
| `workout.py` | Daily workout/reward state machine (calendar-day rollover, wall-clock-capped credit, 25-min floor, √x·ln(x)·0.9 reward) |
| `work.py` | Daily *focus-session* state machine (button start/pause/resume timer, common min across cards, monotonic min, penalty on failure, block-e window) |
| `db.py` | sqlite schema/helpers (`limits` table incl. one-directional add-only domain append) |
| `index.html` | page 1 — limits + bypass override (Home nav button) |
| `workout.html` | page 2 — daily workout card (Workout nav button) |
| `work.html` | page 3 — daily work focus session (Work nav button): cards, common min, timer, start/pause/resume, penalty |
| `secondary_index.html` | child view served by `secondary.py` — nuclear/offline status, READ-ONLY central limits view, stats, workout nav |
| shared `.topnav` | links between the pages; highlights the current page. No JS routing — static HTML files + routes in app.py/secondary.py.
| `remote_store.py` | **NEW** — `CacheDB` (local copy of central `limits`) + `RemoteStore` (cache-first / staleness-TTL / offline-emergency semantics) |
| `secondary.py` | **NEW** — the secondary gateway: local DoH front-door + bypass watcher + sync ticker + child web UI, resolving all limits via `RemoteStore` (synced from central) |
| `run_central.py` | **NEW** — thin launcher → `app.py` in central-only mode. Wraps the old `--no-sinkhole --no-doh --no-bypass` flag set so the central role is never hand-typed |
| `run_secondary.py` | **NEW** — thin launcher → `secondary.py` (symmetric role entrypoint) |
| `test_remote_store.py` | **NEW** — `RemoteStore` unit/integration tests vs a live central-only `app.py` (sync, cache-copy, staleness re-fetch, `report_access`, offline emergency) |
| `test_secondary.py` | **NEW** — end-to-end: central(central-only) + stub upstream + secondary driving real DoH wire queries (blocked/forwarded, detect arms grace centrally) |
| `test_work.py` | Work/focus-session feature tests (unit for the state machine + live end-to-end: work session blocks every domain through the real resolver, penalty window, un-block on completion) |
| `tools/` | Setup & maintenance scripts (see §5) — incl. `limiter-central.service` + `install_central.sh` |
| `test_limit.py` | Per-limit, one-directional (add-only) domain management: unit (`Store.add_domains`) + live HTTP integration (create/append/dedup + the guarantee that no delete/remove-domain endpoint exists) |
| `test_*.py` | 9 test suites (engine, dns, doh, app, bypass, remote_store, secondary, work, limit) — plain `python3 test_x.py` |
| `certs/` | Self-signed CA + server cert for the DoH endpoint |
| `data/` | Runtime state: `limits.db`, `workout.json`, `gateway.log`, pidfile, `browsers_locked.txt` |
| `HANDOFF_CONTEXT.md` | Detailed state notes (architecture, gotchas, session history) |

---

## 3. Installation (new machine, ~10 min)

Assumes Ubuntu, a local Firefox (snap is fine and tested), a user account that
sudo-able (here: `pietro`), and the project at `~/limiter`.

```bash
# 0) deps (python3 stdlib only; certutil comes with Firefox)
sudo apt install python3

# 1) TLS certs for the DoH endpoint (once)
bash tools/gen_certs.sh

# 2) point Firefox at the gateway + import the CA (per profile, idempotent)
bash tools/setup_firefox.sh

# 3) LOCK the DoH settings in Firefox (needs sudo; greyes out the UI, pins provider)
sudo bash tools/install_policy.sh
#    → writes /etc/firefox/policies/policies.json
#    → then QUIT Firefox completely and reopen;
#      verify: about:policies shows it Active; DoH section in settings is read-only

# 4) one-time: let the gateway auto re-lock other browsers hourly (no password)
sudo bash tools/enable_hourly_lock.sh

# 5) run the gateway
bash tools/start.sh
```

> **This machine additionally has a systemd user service** (`~/.config/systemd/user/limiter.service`,
> enabled + lingering): the gateway starts at boot and restarts itself on crash —
> no manual `start.sh` after a reboot. Manage it with
> `systemctl --user {status|restart|stop|start} limiter`. If you prefer plain
> scripts, disable lingering/the unit and use `tools/{start,stop}.sh`.

Then open **http://192.168.x.y:8080** (the Pi's LAN IP) and add limits.
Restart Firefox after step 2/3 so the profile settings apply.

---

## 4. Day-to-day

* **Add a limit**: web UI → name, type, domains. `time` type also takes grace
  (minutes of freedom after first click-through) and cooldown (minutes blocked).
* **Bypass detected**: red banner appears (VPN / public DoH / lock tampered).
  Unblock-all until the cause is fixed, or click **Override** if you dealt with
  it. State + reason visible in `/api/bypass`.
* **Work**: the Work page has a button timer (start / pause / resume), work cards of
  different kinds sharing one common minimum, and a penalty. Start a session while
  working — every site is blocked (focus mode); reach the minimum to finish and the
  block clears, or let the timer run out and a penalty window blocks everything until
  it clears.
* **Workout**: big timer on the UI. Hover it to run; move away to pause.
  Before the first hover you may pick a longer duration (minimum **25 min**);
  hovering locks the choice until 00:00. The timer must reach **zero** — a
  partial session earns nothing. At zero → **REDEEM** button → all blocks
  (limits AND nuclear) suspended `√x·ln(x)·0.9` minutes (x = workout minutes)
  → auto-reverts. Once per day; everything renews at local **00:00**.
  Progress + pending rewards survive gateway restarts (`data/workout.json`,
  in-memory window restored at boot).
* **Other browsers locked**: automatic hourly (log: `[lock] hourly browser relock …`).
  After an `apt upgrade` that touches a locked browser, run
  `sudo bash tools/lock_browsers.sh --relock`. Preview with `--dry-run`.

## 5. Script reference (`tools/`)

| Script | What it does | When |
|---|---|---|
| `start.sh` | starts the gateway (refuses if :8080 is taken by an orphan) | manual mode, or `systemctl --user start limiter` under the service |
| `stop.sh` | stops any running Control Limits process (sweeps `app.py`/`run_central.py`/`run_secondary.py`) | manual / service teardown |
| `install_central.sh` | installs the CENTRAL unit (`limiter-central.service` runs `run_central.py`) + starts it (auto at boot) | once, on the central
| `install_secondary.sh` | installs the SECONDARY gateway on a device (writes `data/secondary.json`, ensures certs, systemd --user enable+start `limiter-secondary.service`) | once, per controlled device |
| `stop_secondary.sh` | stops/disables the secondary unit (`--keep-unit` to stop only) | teardown |
| `gen_certs.sh` | generates the self-signed CA + server cert for DoH | once |
| `setup_firefox.sh` | per profile: writes the managed DNS block into `user.js` (read-only), imports the CA, records profiles, reports policy state | first run, after a Firefox reinstall, new profile |
| `fix_browser_doh.sh` | one-shot cleanup: removes accidental manual DoH settings (e.g. a typed-in `custom_uri`) | only if the child broke the DoH section |
| `install_policy.sh` | installs the Firefox enterprise policy (DoH locked to our gateway) to `/etc/firefox/policies/policies.json` (sudo) | once |
| `lock_browsers.sh` | disables every browser except Firefox (snap disable / chmod 000); modes: default lock, `--dry-run`, `--unlock`, `--relock` (sudo for lock) | usually automatic; manually after apt changes |
| `enable_hourly_lock.sh` | one-time passwordless-sudo rule (visudo-validated) so the gateway can run the browser lock hourly; revoke with `rm /etc/sudoers.d/limiter-lock` | once |
| `get_certutil.sh` | helper if `certutil` isn't on PATH (snap Firefox) | rarely |

## 5b. Role launchers (`run_central.py` / `run_secondary.py`)

Don't hand-type the central's `--no-sinkhole --no-doh --no-bypass` flags. Use the
thin launchers instead — they wrap that exact flag set and keep the two roles
easily tellable apart:

```bash
# CENTRAL (one box, holds limits + admin UI; nobody browses through it)
python3 run_central.py --web 8080 --db data/limits.db
#   (or, once, on the central):
sudo bash tools/install_central.sh      # installs + enables limiter-central.service

# SECONDARY (every other controlled device)
bash tools/install_secondary.sh http://<central-ip>:8080
#   (or run it directly, equivalent to the unit's ExecStart):
python3 run_secondary.py --central http://<central-ip>:8080
```

`run_central.py` is just `app.py` in central-only mode; `run_secondary.py` is
just `secondary.py`. Both exist purely so the "which binary am I, and is the
central really off its front-doors" question is impossible to get wrong.

## 6. Ports, config, state

| Port | Purpose |
|---|---|
| 8443 (TLS, 127.0.0.1 only) | DoH front-door — Firefox points here (`/dns-query`) |
| 5354 (UDP+TCP, LAN) | plain-DNS sinkhole channel (tests / legacy / future whole-OS redirect) |
| 8080 (LAN) | web UI + JSON API |

Environment variables (all optional; defaults in brackets): `WORKOUT_SECONDS [1500]`
(new-day default workout length, seconds; clamped to the floor),
`MIN_WORKOUT_MS [1500000]` (hard 25-minute floor, ms — lowered only for tests),
`DNS_PORT [5354]`, `WEB_PORT [8080]`, `DOH_PORT [8443]`, `DNS_UP [127.0.0.53]`,
`LAN_GW [192.168.0.1]`, `WORKOUT_FILE`, `WORK_FILE`, `PYTHONUNBUFFERED`.
The reward length is NOT configurable — it is the hardcoded √x·ln(x)·0.9 ratio.

Work/focus-session env vars (optional; defaults in brackets), analogous to the workout:
`MIN_WORK_BUDGET_MS [1200000]` (hard floor of the max timer, ms — lowered only for tests),
`MIN_WORK_MIN_MS [600000]` (hard floor of the common minimum, ms),
`WORK_BUDGET_MS [3600000]` (default max timer for the day, ms),
`WORK_MIN_MS [1800000]` (default common minimum for the day, ms),
`WORK_PALTY_MS [1800000]` (default penalty window length on failure, ms).

State files: `data/limits.db` (limits), `data/workout.json` (daily workout/reward), `data/work.json` (daily work focus-session/penalty),
`data/browsers_locked.txt` (what the browser lock touched), `data/gateway.log`
(nohup-run logs; under systemd the log is the journal: `journalctl --user -u limiter`).

API (JSON): `GET /api/limits`, `POST /api/limits`, `DELETE /api/limits/<id>`,
`GET /api/limits/<id>` (single limit — feeds /limit/<id>),
`POST /api/limits/<id>/domains {domains:[...]}` (ADD domains only — one-directional, see §7),
`GET /api/bypass`, `POST /api/bypass/override {enabled}`, `GET /api/stats`,
`GET /api/workout`, `POST /api/workout {action: start|tick|redeem|set_duration {ms}}`,
`GET /api/work`, `POST /api/work {action: add_card|remove_card|set_budget|set_limit|set_active_card|start|pause|resume|tick}`, `GET /api/health`.

## 7. Security notes & known limits (be honest about these)

* **Web UI is three pages** (`index.html` + `workout.html` + `work.html`) joined by a top nav bar
  that switches between them. The server serves `index.html` at `/`,
  `workout.html` at `/workout.html` and `work.html` at `/work.html` (all static;
  `/api/…` serve live data).
* **The web UI has no authentication.** It is trusted-LAN by design: anyone on
  the LAN can read/modify limits, redeem the reward (the workout's anti-cheat
  limits are real-time-capped server-side, but a LAN client can POST ticks at
  1 real second per credit-second — so the reward costs ~the workout duration
  per click… it cannot be fast-forwarded) and override nuclear. If the LAN is
  not fully trusted, put the Pi on a separate VLAN/SSID from the child.
* **Escape paths remaining** (documented, not hidden): a process that connects
  to the internet *without DNS* (hardcoded IPs), or an OS-level DNS redirect
  done with root. The `--bypass-protect` iptables layer and a whole-OS
  plaintext-DNS redirect (tools/redirect_dns.sh — not yet built) close most of
  the rest.
* **Browser lock** can be reverted only with root; an apt upgrade of a locked
  package can restore the binary (re-run `--relock`).
* **Reward semantics**: suspends limits *and* nuclear for
  `√x·ln(x)·0.9` min (x = chosen workout minutes); same-day only,
  non-stackable, earned only on full completion, restored across gateway
  restarts while active.
* **Limit domains are one-directional (add-only):** `POST /api/limits/<id>/domains`
  appends domains to a limit, but there is no endpoint to remove a domain once added.
  Undoing an add is only possible by hand-editing the hidden `data/limits.db`
  (WAL-mode, outside the web root) — treat added domains as permanent.

* Headless snap Firefox cannot be used to *validate* DoH in this environment
  (broken DNS path in headless mode) — validate in the real browser.

## 8. Tests

```bash
systemctl --user stop limiter        # :5354 must be free for the stub upstreams
for t in test_engine test_dns test_doh test_app test_bypass test_remote_store test_work test_limit; do python3 $t.py; done
systemctl --user start limiter
```
All nine suites are self-contained (throwaway sqlite in /tmp, fake upstreams).
`test_secondary` and `test_remote_store` also stand up a **central-only** `app.py`
(and a stub upstream) in-process; they clean up after themselves. `test_remote_store`
kills any stray process already bound to its ports before launching. `test_bypass.py`
includes a LIVE scan for public-DoH sockets — if anything on the machine genuinely
uses public DoH while it runs, that test fails (on purpose: it doubles as the
security alarm).

> Heads-up: the central-only `app.py` used by the new tests binds `:8080`; running
> the new tests while the live gateway holds :8080 will make `test_app` hit the
> *live* DB instead of its own — that is why the loop above stops the gateway first.

## 9. Machine state at handoff (2026-10-16 — after central/secondary rearch)

* **All 9 test suites green.** Nothing in the original single-host engine regressed.
  The **per-limit domain-management** feature (click a limit → dedicated page
  `limit.html` at `/limit/<id>`; see the detail page for the limit's type /
  conditions / domains and an *add domains* control) is implemented and tested
  (`test_limit.py`). It is wired via `db.append_domains_to_limit` + `Store.add_domains`,
  and is one-directional: `POST /api/limits/<id>/domains` only appends — there is no
  remove/delete-domain endpoint, so undoing an add needs a hand-edit of the hidden DB.
* **Work / focus-session feature is implemented and tested** (new `work.py`, `work.html`, `test_work.py`; wired into `app.py` and `secondary.py` via a new block-e window on the store). A session blocks every domain through the real resolver, the minimum is monotonic, and a failed session arms a penalty window. See README §1 + the `work.py` module doc.
* Single-host `limiter.service` still **running** (systemd --user, lingering → auto
  at boot) on :8443/:5354/:8080. This is the *old* mode and still works, but for the
  new central/secondary topology the **central** should be run in central-only mode:
  `python3 app.py --no-sinkhole --no-doh --no-bypass --web 8080 --db data/limits.db`.
* **Secondary** is not yet installed anywhere (it is installed per-device on demand via
  `tools/install_secondary.sh <central-url>`). All tooling (`secondary.py`,
  `remote_store.py`, `tools/limiter-secondary.service`, `install/stop_secondary.sh`,
  `secondary_index.html`) is in place and tested.
* `certs/` present and shared by both the central and every secondary.
* Live limits: the running gateway holds its DB limits (incl. prom.ua).

* Next human step: pick one box as the **central** (run it central-only), then run
  `install_secondary.sh <central-ip>:8080` + `setup_firefox.sh` on each controlled device.

## 10. Pending (not yet done)

* `sudo bash tools/install_policy.sh` (+ Firefox restart) on each device to lock
  Firefox's DoH settings read-only so the child can't switch to Cloudflare/Google.
* Optional whole-OS plain-DNS control (`bypass.py` already generates the ruleset;
  needs root). Treat as dormant.
* Optional polish / refactors the user is reviewing by hand — nothing blocks shipping.
* Verify on real second-device hardware (all flows proven in-process via
  `test_secondary`).
