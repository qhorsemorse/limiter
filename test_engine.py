import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import engine as E

def t(grace_min, cooldown_min):
    return {"id":1,"name":"g","type":"time","domains":["www.y.com","tiktok.com"],
            "grace_ms":E._ms(grace_min),"cooldown_ms":E._ms(cooldown_min),
            "state":"free","grace_until":None,"cooldown_until":None}

# 1) detection arms grace
lim = t(40,120); now = 0
assert E.evaluate([lim],"www.y.com") == (False,None), "should be free initially"
E.detect_access(lim, now)
assert lim["grace_until"] == now + lim["grace_ms"], "grace arms now+grace_ms"
assert E.evaluate([lim],"tiktok.com") == (False,None), "group mate also free"

# 2) during grace, free
assert E.evaluate([lim],"www.y.com") == (False,None)
E.tick([lim], now + 20*60_000)
assert lim["state"]=="free" and lim["grace_until"] is not None

# 3) grace expires -> blocked (whole group)
E.tick([lim], now + 41*60_000)
assert lim["state"]=="blocked", lim
assert E.evaluate([lim],"www.y.com")[0] is True
assert E.evaluate([lim],"tiktok.com")[0] is True, "group mate blocked too"

# 4) cooldown expires -> free again
E.tick([lim], now + 41*60_000 + 121*60_000)
assert lim["state"]=="free", lim
assert E.evaluate([lim],"www.y.com") == (False,None)

# 5) detection only arms once during freedom (no infinite freedom)
lim = t(40,120); now=1000
E.detect_access(lim, now); g1=lim["grace_until"]
E.detect_access(lim, now+1000)  # access again in grace
assert lim["grace_until"]==g1, "grace should NOT re-arm during freedom"

# 6) absolute always blocks, independent of time limits
abs_lim={"id":2,"name":"a","type":"absolute","domains":["prom.ua"]}
assert E.evaluate([abs_lim],"prom.ua")[0] is True
assert E.evaluate([abs_lim],"www.prom.ua")[0] is True, "subdomain/www should match"
assert E.evaluate([abs_lim],"api.prom.ua")[0] is True, "nested subdomain should match"
assert E.evaluate([abs_lim],"notprom.ua")[0] is False, "notprom.ua should not match"
assert E.evaluate([abs_lim],"other.com")[0] is False

# 6b) www in limit matches apex
www_lim={"id":3,"name":"w","type":"absolute","domains":["www.example.org"]}
assert E.evaluate([www_lim],"example.org")[0] is True, "apex should match www limit"
assert E.evaluate([www_lim],"www.example.org")[0] is True
assert E.evaluate([www_lim],"sub.example.org")[0] is True

# 7) group: opening ONLY one domain blocks the whole group after grace
lim = t(40,120)
E.detect_access(lim, 0)   # only youtube accessed
E.tick([lim], 41*60_000)
assert E.evaluate([lim],"tiktok.com")[0] is True, "group mate blocked even though only yt accessed"

# 8) formatting
assert E.fmt(3_600_000)=="01:00:00", E.fmt(3_600_000)
assert E.fmt(90_000)=="00:01:30", E.fmt(90_000)
assert E.fmt(0)=="00:00:00"

# 9) normalization
assert E.normalize_domain("https://www.Y.com/path?x=1")=="www.y.com"
assert E.normalize_domain("prom.ua/")=="prom.ua"
assert E.normalize_domain("sub.api.v.co.uk")=="sub.api.v.co.uk"

print("ALL ENGINE TESTS PASSED")
