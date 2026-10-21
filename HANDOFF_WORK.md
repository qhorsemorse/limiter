# HANDOFF PROMPT — "Control Limits" (v5 — central + secondary architecture, ~2026-10-16)

=== BEGIN HANDOFF PROMPT ===

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
                 ├─ work          (per-device, local) — focus-session block-e window + timer, honours store.work_active(now)
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
| `store.py` / `remote_store.py` | Added `work_until`, `set_work_until(ms)`, `work_active(now)` — a per-session block-e window, synced from the Work state machine after each mutating `/api/work` action and restored on boot. |
| `app.py` | Serves `/work.html`; `GET/POST /api/work` (add/remove/set_card/start/pause/resume/tick); restores the work block-e window on boot via `set_work_until`. |
| `secondary.py` | Serves `/work.html`; `GET/POST /api/work`; per-device Work state machine; syncs `set_work_until` onto the `RemoteStore` so the block-e is enforced on the device. |
| `index.html` / `workout.html` / `secondary_index.html` | Nav bar gains a **Work** link. |

**NEW this session (work feature):**

| File | Role | Status |
|---|---|---|
| `work.py` | Daily focus-session state machine: button start/pause/resume timer (cheat-capped like workout), multiple cards sharing one common monotonic minimum, per-day reset, penalty window on failure, and the `work_active` window the resolver honours. Persists to `data/work.json`. | done, tested |
| `work.html` | Page 3 — cards, the common minimum, the timer, and the start/pause/resume button + penalty display. | done |
| `test_work.py` | Work feature tests: unit for the state machine + live end-to-end through the real DNS resolver (session blocks every domain, penalty window, un-block on completion). | green |

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

All 7 suites are plain-Python (no pytest), self-contained (throwaway sqlite in /tmp,
fake upstreams). `test_dns` and `test_app` bind their stub upstream on **:5354**, which
the *live* gateway also binds — run them with the gateway stopped:
```bash
systemctl --user stop limiter                 # free :5354 (and :8080) for the stubs
for t in test_engine test_dns test_doh test_app test_bypass test_remote_store test_secondary; do python3 $t.py; done
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
- **Work block-e window (`store.work_active(now)`)** sits in `dns.resolve_bytes` RIGHT AFTER the reward check and BEFORE nuclear — it is the inverse of the workout reward: while a work session (or penalty) is active, EVERY domain (time-limit AND absolute) resolves to `0.0.0.0` (localhost exempt). Set/clear it via `set_work_until(ms)` and re-arm it on boot. It is a per-device LOCAL concern on the secondary (synced onto the `RemoteStore`), like the workout — the central does not own it.
- **Work minimum is monotonic** (set at the first card's creation, can only grow) and **work timer is capped by real elapsed wall-clock** between ticks (cheat-resistance, same as the workout) — never trust a big client-reported delta.
- `index.html`/`secondary_index.html` are served stale until the browser refreshes.
- Fail-closed offline is the default; `--allow-free-offline` flips it. Locks block always.

=== END HANDOFF PROMPT ===

## NEXT STEPS (in priority order)
1. **Work / focus-session feature — DONE (this session).** New `work.py` + `work.html` + `test_work.py`, wired into `app.py` and `secondary.py`, with a store-level block-e window (`work_active`) that the resolver honours. All 8 suites green. Behaviour: a session blocks every domain (time-limit AND absolute) while active, has a button start/pause/resume timer, cards sharing one common monotonic minimum, and arms a penalty window when the timer runs out before the minimum is met. See README §1 + the `work.py` module doc for the exact rules.
2. **Live end-to-end proof on real hardware.** Everything is proven in-process
   (test_secondary drives real DoH wire queries through the secondary). The one thing
   left is a real Firefox on a second device pointed at a secondary (steps in §5). If a
   blocked domain still loads, the usual causes: certs missing (DoH off), lock not
   applied on THAT device (Firefox not routing through us → watcher may go nuclear,
   which is still blocking), or upstream forward broken.
2. **`install_policy.sh` on the user side** (sudo) to lock Firefox's DoH settings
   read-only on each secondary, so the child can't switch to Cloudflare/Google.
3. **Optional — whole-OS plain-DNS control** on secondaries (`bypass.py` already
   generates the ruleset; needs root). Treat as dormant.
4. **Optional polish / refactors** the user is currently reviewing by hand — nothing
   blocks shipping.
