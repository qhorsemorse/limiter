"""SQLite persistence for limits. The db file lives OUTSIDE the web root
so it can never be fetched over HTTP (it is 'hidden')."""
import json, os, sqlite3, time

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "data", "limits.db")

def _connect(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode = WAL")
    return con

def init_db(path=DEFAULT_DB):
    con = _connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, type TEXT, domains TEXT,
        grace_ms INTEGER, cooldown_ms INTEGER,
        state TEXT, grace_until REAL, cooldown_until REAL,
        created_at REAL,
        updated_at REAL
    )""")
    # Migrate an older DB (created before updated_at existed): add the column
    # and stamp existing rows with their created_at so secondaries can still
    # reason about staleness.
    cols = {r[1] for r in con.execute("PRAGMA table_info(limits)")}
    if "updated_at" not in cols:
        con.execute("ALTER TABLE limits ADD COLUMN updated_at REAL")
        for row in con.execute("SELECT id, created_at FROM limits"):
            con.execute("UPDATE limits SET updated_at=? WHERE id=?", (row[1], row[0]))
    con.commit()
    return con

def add_limit(con, rec):
    now = time.time()
    c = con.execute(
        "INSERT INTO limits(name,type,domains,grace_ms,cooldown_ms,state,grace_until,cooldown_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rec.get("name",""), rec["type"], json.dumps(rec["domains"]),
         rec.get("grace_ms"), rec.get("cooldown_ms"), "free", None, None, now, now),
    )
    con.commit()
    return c.lastrowid

def delete_limit(con, lid):
    cur = con.execute("DELETE FROM limits WHERE id=?", (lid,))
    con.commit()
    return cur.rowcount

def append_domains_to_limit(con, lid, domains, now=None):
    """Append `domains` (a list of already-normalized hostnames) to limit `lid`,
    deduping against the limit's existing domains and against each other. A domain
    that is already present is left exactly where it is -- it can NEVER be moved or
    removed. Returns the number of NEW domains added (0 means everything was
    already there).

    This is the one-directional (add-only) part of the per-limit flow: there is no
    code path that ever removes a domain from a limit. Undoing an add is only
    possible by hand-editing this (hidden) DB."""
    row = con.execute("SELECT domains FROM limits WHERE id=?", (lid,)).fetchone()
    if row is None:
        return None                     # limit vanished (shouldn't happen via API)
    existing = json.loads(row[0] or "[]")
    existing_ids = set(existing)
    added = [d for d in domains if d not in existing_ids]
    if added:
        existing.extend(added)
        if now is None:
            now = time.time()
        con.execute("UPDATE limits SET domains=?, updated_at=? WHERE id=?",
                    (json.dumps(existing), now, lid))
        con.commit()
    return len(added)

def get_limit(con, lid):
    row = con.execute("SELECT * FROM limits WHERE id=?", (lid,)).fetchone()
    return _row_to_dict(row) if row else None

def list_limits(con):
    out = []
    for row in con.execute("SELECT * FROM limits ORDER BY id"):
        out.append(_row_to_dict(row))
    return out

def save_state(con, lid, state, grace_until, cooldown_until, updated_at=None):
    """Persist a limit's state machine fields. Also stamps updated_at so
    remote secondaries know when the row last changed (their staleness / TTL
    check keys off this column)."""
    if updated_at is None:
        updated_at = time.time()
    con.execute("UPDATE limits SET state=?, grace_until=?, cooldown_until=?, updated_at=? WHERE id=?",
                (state, grace_until, cooldown_until, updated_at, lid))
    con.commit()

def _row_to_dict(row):
    cols = row.keys if hasattr(row, "keys") else range(len(row))
    d = dict(zip(row.keys, row)) if hasattr(row, "keys") else dict(zip(
        ["id","name","type","domains","grace_ms","cooldown_ms",
         "state","grace_until","cooldown_until","created_at"], row))
    d["domains"] = json.loads(d.get("domains","[]") or "[]")
    return d
