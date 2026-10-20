"""Core limit logic. Pure / no side effects -> easy to unit test.

A *limit* is a plain dict:
  {
    "id": int,
    "name": str,
    "type": "absolute" | "time",
    "domains": [str, ...],        # normalized hostnames
    # time limits:
    "grace_ms": int,              # freedom window after detection
    "cooldown_ms": int,           # blocked window
    "state": "free" | "blocked",
    "grace_until": float|None,    # epoch ms; when grace ends
    "cooldown_until": float|None, # epoch ms; when cooldown ends
  }
"""
import time, re

_ms = lambda minutes: int(minutes * 60_000)
now_ms = lambda: time.time() * 1000.0

# Domains that must NEVER be sinkholed, even in nuclear (bypass) mode:
# the admin UI lives on localhost and the gateway must stay reachable.
NUCLEAR_EXEMPT = {"localhost", "ip6-localhost", "ip4-localhost"}

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$", re.I)

def normalize_domain(d):
    """Turn 'https://www.X.com/path' / 'X.com/' -> 'www.x.com'."""
    d = (d or "").strip().lower().lstrip("/")
    d = re.sub(r"^(https?://)?", "", d)        # drop scheme
    d = d.split("/", 1)[0]                      # drop path
    d = d.split(":", 1)[0]                      # drop :port
    d = d.split("@", 1)[-1]                     # drop user@
    d = d.strip(".").strip()                    # trim dots
    return d

def is_valid(d):
    return bool(_DOMAIN_RE.match(d)) if d else False

def valid_domains(domains):
    out = []
    for d in domains:
        n = normalize_domain(d)
        if is_valid(n) and n not in out:
            out.append(n)
    return out

# ------------------------ detection & state machine ------------------------
def detect_access(limit, now):
    """Called when a domain in this time-limit was actually reached.
    Arms the freedom timer once (first detection of a free cycle)."""
    if limit["type"] != "time":
        return
    if limit["state"] == "free" and limit["grace_until"] is None:
        limit["grace_until"] = now + limit["grace_ms"]  # freedom ends here

def tick(limits, now):
    """Advance any limit whose timers have elapsed. Mutates + returns limits."""
    for lim in limits:
        if lim["type"] != "time":
            continue
        # free -> blocked (grace expired)
        if (lim["state"] == "free" and lim["grace_until"] is not None
                and now >= lim["grace_until"]):
            lim["state"] = "blocked"
            lim["grace_until"] = None
            lim["cooldown_until"] = now + lim["cooldown_ms"]
        # blocked -> free (cooldown expired)
        elif (lim["state"] == "blocked" and lim["cooldown_until"] is not None
              and now >= lim["cooldown_until"]):
            lim["state"] = "free"
            lim["grace_until"] = None
            lim["cooldown_until"] = None
    return limits

def _domain_matches(pattern, target):
    p = pattern.lower().strip()
    t = target.lower().strip()
    if p == t:
        return True
    if t.endswith("." + p):
        return True
    if p.startswith("www."):
        base = p[4:]
        if t == base or t.endswith("." + base):
            return True
    return False

def _covers(lim, domain):
    return any(_domain_matches(d, domain) for d in lim.get("domains", []))

def evaluate(limits, domain):
    """Return (blocked: bool, matched_limit_id_or_None)."""
    d = normalize_domain(domain)
    for lim in limits:
        if lim["type"] == "absolute" and _covers(lim, d):
            return True, lim["id"]
    for lim in limits:
        if lim["type"] == "time" and _covers(lim, d) and lim["state"] == "blocked":
            return True, lim["id"]
    return False, None

# ------------------------ display helpers ------------------------
def remaining(lim, now):
    """Human-ish info for the frontend."""
    if lim["type"] == "absolute":
        return {"state": "blocked", "label": "Always blocked",
                "until": None, "left_ms": 0}
    if lim["state"] == "free":
        if lim["grace_until"] is not None:
            left = lim["grace_until"] - now
            return {"state": "free", "label": "Freedom left -> blocked in",
                    "until": lim["grace_until"], "left_ms": max(0, left),
                    "until_end": True}
        return {"state": "free", "label": "Free (access to start timer)",
                "until": None, "left_ms": 0}
    # blocked
    if lim["cooldown_until"] is not None:
        left = lim["cooldown_until"] - now
        return {"state": "blocked", "label": "Blocked -> free in",
                "until": lim["cooldown_until"], "left_ms": max(0, left),
                "until_end": True}
    return {"state": "blocked", "label": "Blocked", "until": None, "left_ms": 0}

def fmt(ms):
    """ms -> HH:MM:SS string."""
    ms = int(round(ms))
    if ms < 0:
        ms = 0
    total_s = ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return "%02d:%02d:%02d" % (h, m, s)
